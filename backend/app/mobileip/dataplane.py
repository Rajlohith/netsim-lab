"""Data-plane interfaces: what the protocol cores ask the 'network' to do.

The cores never touch the kernel. Implementations:
  * Recording*        - in-memory (tests)
  * Sim*              - in simnet.py (pure-Python forwarding)
  * Handshake*        - runs INSIDE a namespace as an unprivileged user: writes requests to a file and waits
                        for an ack; a DataPlaneController (outside, using sudo -n per command) applies them
  * linux_plan        - the pure command builders the controller uses (ip tunnel / route / addr)
"""
from __future__ import annotations

import ipaddress
import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path


class DataPlaneError(RuntimeError):
    pass


class HaDataPlane:
    def install_binding(self, home, coa): ...
    def remove_binding(self, home): ...


class MnDataPlane:
    def set_care_of(self, coa): ...
    def clear(self): ...


class RecordingHaDataPlane(HaDataPlane):
    def __init__(self, fail=False):
        self.calls, self.tunnels, self.fail = [], {}, fail

    def install_binding(self, home, coa):
        if self.fail:
            raise DataPlaneError("injected data-plane failure")
        self.calls.append(("install", str(home), str(coa)))
        self.tunnels[ipaddress.IPv4Address(home)] = ipaddress.IPv4Address(coa)

    def remove_binding(self, home):
        self.calls.append(("remove", str(home)))
        self.tunnels.pop(ipaddress.IPv4Address(home), None)


class RecordingMnDataPlane(MnDataPlane):
    def __init__(self):
        self.calls, self.care_of = [], None

    def set_care_of(self, coa):
        self.calls.append(("set_care_of", str(coa)))
        self.care_of = ipaddress.IPv4Address(coa)

    def clear(self):
        self.calls.append(("clear",))
        self.care_of = None


# ---------------------------------------------------------------------------------------------
# Linux command plan (iproute2). Used by the controller; every command is allow-listed in netem.safety.
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LinuxMipConfig:
    prefix: str = "nsl-"
    ha_node: str = "ha"
    mn_node: str = "mn"
    agent_address: str = "10.10.100.1"       # HA address on the (virtual) home network, on HA's lo
    home_address: str = "10.10.100.2"        # MN home address, on MN's lo
    ha_tunnel: str = "tnl-mn"
    mn_tunnel: str = "tnl-ha"

    @property
    def ha_ns(self):
        return f"{self.prefix}{self.ha_node}"

    @property
    def mn_ns(self):
        return f"{self.prefix}{self.mn_node}"


def _cmd(argv, desc):
    from ..netem.plan import Cmd
    return Cmd(tuple(argv), desc)


def linux_setup_home(c: LinuxMipConfig):
    return [
        _cmd(("ip", "-n", c.ha_ns, "addr", "add", f"{c.agent_address}/32", "dev", "lo"), "HA agent address on lo"),
        _cmd(("ip", "-n", c.mn_ns, "addr", "add", f"{c.home_address}/32", "dev", "lo"), "MN home address on lo"),
    ]


def linux_teardown_home(c: LinuxMipConfig):
    return [
        _cmd(("ip", "-n", c.ha_ns, "addr", "del", f"{c.agent_address}/32", "dev", "lo"), "remove HA agent address"),
        _cmd(("ip", "-n", c.mn_ns, "addr", "del", f"{c.home_address}/32", "dev", "lo"), "remove MN home address"),
    ]


def linux_ha_install(c: LinuxMipConfig, coa, exists: bool):
    verb = "change" if exists else "add"
    cmds = [_cmd(("ip", "-n", c.ha_ns, "tunnel", verb, c.ha_tunnel, "mode", "ipip", "local", c.agent_address,
                  "remote", str(coa)), f"HA tunnel {verb} -> {coa}")]
    if not exists:
        cmds.append(_cmd(("ip", "-n", c.ha_ns, "link", "set", c.ha_tunnel, "up"), "HA tunnel up"))
    cmds.append(_cmd(("ip", "-n", c.ha_ns, "route", "replace", f"{c.home_address}/32", "dev", c.ha_tunnel),
                     "route home address into the tunnel"))
    return cmds


def linux_ha_remove(c: LinuxMipConfig):
    return [_cmd(("ip", "-n", c.ha_ns, "route", "del", f"{c.home_address}/32"), "unroute home address"),
            _cmd(("ip", "-n", c.ha_ns, "tunnel", "del", c.ha_tunnel), "delete HA tunnel")]


def linux_mn_set_care_of(c: LinuxMipConfig, coa, exists: bool):
    verb = "change" if exists else "add"
    cmds = [_cmd(("ip", "-n", c.mn_ns, "tunnel", verb, c.mn_tunnel, "mode", "ipip", "local", str(coa),
                  "remote", c.agent_address), f"MN tunnel {verb} local {coa}")]
    if not exists:
        cmds.append(_cmd(("ip", "-n", c.mn_ns, "link", "set", c.mn_tunnel, "up"), "MN tunnel up"))
    return cmds


def linux_mn_clear(c: LinuxMipConfig):
    return [_cmd(("ip", "-n", c.mn_ns, "tunnel", "del", c.mn_tunnel), "delete MN tunnel")]


# ---------------------------------------------------------------------------------------------
# File handshake between an unprivileged in-namespace agent and the privileged-per-command controller
# ---------------------------------------------------------------------------------------------
class _Handshake:
    def __init__(self, req_path: Path, ack_path: Path, timeout_s: float = 3.0, poll_s: float = 0.002):
        self.req_path, self.ack_path = Path(req_path), Path(ack_path)
        self.timeout_s, self.poll_s, self._n = timeout_s, poll_s, 0
        self._nonce = secrets.token_hex(4)          # ids are unique per instance, so a shared file cannot confuse acks

    def _call(self, op: str, **kw):
        self._n += 1
        rid = f"{self._nonce}-{self._n}"
        line = json.dumps({"id": rid, "op": op, **kw}) + "\n"
        with open(self.req_path, "a") as f:
            f.write(line)
            f.flush()
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if self.ack_path.exists():
                for raw in self.ack_path.read_text().splitlines():
                    try:
                        ack = json.loads(raw)
                    except ValueError:
                        continue
                    if ack.get("id") == rid:
                        if not ack.get("ok"):
                            raise DataPlaneError(ack.get("error", "controller refused"))
                        return
            time.sleep(self.poll_s)
        raise DataPlaneError(f"data-plane controller did not acknowledge '{op}' in {self.timeout_s}s")


class HandshakeHaDataPlane(_Handshake, HaDataPlane):
    def install_binding(self, home, coa):
        self._call("ha_install", home=str(home), coa=str(coa))

    def remove_binding(self, home):
        self._call("ha_remove", home=str(home))


class HandshakeMnDataPlane(_Handshake, MnDataPlane):
    def set_care_of(self, coa):
        self._call("mn_set_care_of", coa=str(coa))

    def clear(self):
        self._call("mn_clear")


class DataPlaneController:
    """Applies handshake requests with `apply(op, params) -> None` (raises to refuse). Thread-based."""

    def __init__(self, req_path: Path, ack_path: Path, apply, poll_s: float = 0.002):
        self.req_path, self.ack_path, self.apply, self.poll_s = Path(req_path), Path(ack_path), apply, poll_s
        self._offset, self._stop, self._thread = 0, threading.Event(), None
        self.errors: list = []

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            self.drain()
            time.sleep(self.poll_s)
        self.drain()

    def drain(self):
        if not self.req_path.exists():
            return
        with open(self.req_path) as f:
            f.seek(self._offset)
            data = f.read()
        end = data.rfind("\n") + 1                 # only complete lines
        self._offset += end
        for raw in data[:end].splitlines():
            try:
                req = json.loads(raw)
                params = {k: v for k, v in req.items() if k not in ("id", "op")}
                self.apply(req["op"], params)
                ack = {"id": req["id"], "ok": True}
            except Exception as e:                   # refuse, never crash the controller
                self.errors.append(str(e))
                ack = {"id": req.get("id") if isinstance(req, dict) else None, "ok": False, "error": str(e)}
            with open(self.ack_path, "a") as f:
                f.write(json.dumps(ack) + "\n")
