"""Real-UDP drivers for the protocol cores (the cores themselves are transport-agnostic).

  HomeAgentServer  - binds a UDP socket, feeds datagrams to HomeAgent, ticks binding expiry
  MobileNodeClient - one socket bound to the current care-of address (re-bound on every attach)
CLI: python -m app.mobileip.runtime ha|mn ...  (run inside a namespace as an unprivileged user)
Control plane port defaults to 4340 (RFC 5944 says 434, which needs privileges).
"""
from __future__ import annotations

import argparse
import json
import select
import socket
import sys
import time
from pathlib import Path

from ..traffic import clock as realclock
from ..traffic.recorder import RunMeta
from ..traffic.schema import new_run_id, validate_id
from ..traffic.shutdown import ShutdownFlag
from . import messages as M
from .dataplane import (HandshakeHaDataPlane, HandshakeMnDataPlane, RecordingHaDataPlane,
                        RecordingMnDataPlane)
from .events import MipEventLog
from .home_agent import HomeAgent
from .mobile_node import MobileNode


class RealClock:
    mono_ns = staticmethod(realclock.mono_ns)
    wall_ns = staticmethod(realclock.wall_ns)


class HomeAgentServer:
    def __init__(self, core: HomeAgent, bind: tuple, flag: ShutdownFlag | None = None, tick_s: float = 0.05):
        self.core, self.bind, self.flag, self.tick_s = core, bind, flag or ShutdownFlag(), tick_s
        self.sock = None
        self.port = None
        self.ready = None             # optional callable(port)

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(self.bind)
            self.port = self.sock.getsockname()[1]
            if self.ready:
                self.ready(self.port)
            while not self.flag.requested:
                if select.select([self.sock], [], [], self.tick_s)[0]:
                    try:
                        data, addr = self.sock.recvfrom(2048)
                    except OSError:
                        continue
                    for out in self.core.handle_datagram(data, addr):
                        self.sock.sendto(out.data, out.dst)
                self.core.tick()
        finally:
            self.core.shutdown()          # removes bindings + tunnels even on signal/stop-file
            self.sock.close()


class MobileNodeClient:
    """Synchronous helper: call attach()/deregister(), then step() or wait_state()."""

    def __init__(self, core: MobileNode):
        self.core, self.sock = core, None

    def _bind(self, coa):
        if self.sock:
            self.sock.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((str(coa), 0))

    def _send(self, outs):
        for o in outs:
            self.sock.sendto(o.data, o.dst)

    def attach(self, coa):
        self._bind(coa)
        self._send(self.core.attach(coa))

    def deregister(self):
        self._send(self.core.deregister())

    def step(self, timeout: float = 0.05):
        if self.sock is None:
            time.sleep(timeout)
            return
        dl = self.core.next_deadline_ns()
        if dl is not None:
            timeout = max(0.0, min(timeout, (dl - self.core.clock.mono_ns()) / 1e9))
        if select.select([self.sock], [], [], timeout)[0]:
            try:
                data, addr = self.sock.recvfrom(2048)
                self._send(self.core.handle_datagram(data, addr))
            except OSError:
                pass
        self._send(self.core.poll())

    def wait_state(self, states, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.core.state in states:
                return True
            self.step(0.01)
        return self.core.state in states

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


# ------------------------------------------------------------------ CLI -------------------------------
def _key(path: Path) -> bytes:
    raw = Path(path).read_text().strip()
    try:
        return bytes.fromhex(raw)
    except ValueError:
        return raw.encode()


def _common(p):
    p.add_argument("--scenario-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out-dir", type=Path, default=Path("data/results"))
    p.add_argument("--key-file", type=Path, required=True, help="shared secret (hex or text), never on the command line")
    p.add_argument("--spi", type=int, default=256)
    p.add_argument("--home-address", required=True)
    p.add_argument("--agent-address", required=True)
    p.add_argument("--port", type=int, default=M.DEFAULT_PORT)
    p.add_argument("--stop-file", type=Path)
    p.add_argument("--ready-file", type=Path)
    p.add_argument("--dp-req", type=Path, help="data-plane handshake request file (omit = no data plane)")
    p.add_argument("--dp-ack", type=Path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.mobileip.runtime")
    sub = ap.add_subparsers(dest="role", required=True)
    h = sub.add_parser("ha")
    _common(h)
    h.add_argument("--bind", required=True)
    h.add_argument("--home-network", required=True)
    h.add_argument("--max-lifetime", type=int, default=300)
    m = sub.add_parser("mn")
    _common(m)
    m.add_argument("--control-file", type=Path, required=True, help="JSON lines: {op: attach|deregister, coa}")
    m.add_argument("--status-file", type=Path, required=True)
    m.add_argument("--lifetime", type=int, default=60)
    a = ap.parse_args(argv)
    try:
        validate_id(a.scenario_id), validate_id(a.run_id)
        run_dir = a.out_dir / a.scenario_id / a.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        sa = M.SecurityAssociation(a.spi, _key(a.key_file))
        flag = ShutdownFlag(a.stop_file).install()
        events = MipEventLog(run_dir / f"mip_events.{a.role}.csv", RealClock)
        meta = RunMeta(run_dir / f"mip.{a.role}.meta.json", "sender" if a.role == "mn" else "receiver",
                       a.scenario_id, a.run_id, 0, {"role": a.role, "agent_address": a.agent_address}, {})
        meta.data["tool"] = "netsim-mobileip"
        meta.write()
    except (ValueError, OSError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    status, reason = "failed", "error"
    try:
        if a.role == "ha":
            dp = HandshakeHaDataPlane(a.dp_req, a.dp_ack) if a.dp_req else RecordingHaDataPlane()
            core = HomeAgent(a.agent_address, a.home_network, {a.home_address: sa}, dp, RealClock, events,
                             max_lifetime_s=a.max_lifetime)
            srv = HomeAgentServer(core, (a.bind, a.port), flag)
            srv.ready = lambda port: a.ready_file and a.ready_file.write_text(f"{port}\n")
            srv.run()
        else:
            dp = HandshakeMnDataPlane(a.dp_req, a.dp_ack) if a.dp_req else RecordingMnDataPlane()
            core = MobileNode(a.home_address, a.agent_address, sa, RealClock, dp, events,
                              requested_lifetime_s=a.lifetime, agent_port=a.port)
            cli, offset = MobileNodeClient(core), 0
            if a.ready_file:
                a.ready_file.write_text("ready\n")
            last_state = None
            while not flag.requested:
                if a.control_file.exists():
                    with open(a.control_file) as f:
                        f.seek(offset)
                        data = f.read()
                    end = data.rfind("\n") + 1
                    offset += end
                    for line in data[:end].splitlines():
                        cmd = json.loads(line)
                        if cmd.get("op") == "attach":
                            cli.attach(cmd["coa"])
                        elif cmd.get("op") == "deregister":
                            cli.deregister()
                cli.step(0.02)
                if core.state != last_state:
                    last_state = core.state
                    a.status_file.write_text(json.dumps({"state": core.state, "care_of": str(core.care_of or "")}) + "\n")
            cli.close()
        status, reason = ("interrupted", flag.reason or "stop") if flag.requested else ("completed", "done")
        return 0
    except BaseException as e:
        reason = f"error:{type(e).__name__}:{e}"
        raise
    finally:
        flag.restore()
        events.close()
        meta.finalize(status, reason, {})


if __name__ == "__main__":
    sys.exit(main())
