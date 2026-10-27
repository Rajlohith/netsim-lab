"""Live Mobile IP demo inside the Stage 2 namespace testbed (needs sudo + ip/ipip; NOT runnable in a sandbox).

REAL here: the registration protocol over real UDP sockets between a Home Agent daemon (ns ha) and a Mobile Node
agent (ns mn), and the Linux kernel's IP-in-IP tunnels. All of it runs inside the project's namespaces.
EMULATED here: the "movement" (an interface of the mobile node is brought up/down), the virtual home network
(addresses on `lo`, no ARP/proxy-ARP) and the movement-detection step (the orchestrator tells the MN agent
which care-of address to use). Privileged `ip` commands are executed by this orchestrator on behalf of the
unprivileged daemons through a file handshake (see dataplane.py), so the daemons never run as root.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..netem import plan as netplan
from ..netem.errors import NetemError
from ..traffic import clock
from ..traffic.recorder import EventLog
from ..traffic.schema import new_run_id, validate_id
from . import dataplane as dp
from .analyze import analyze_mip_run
from .applier import LinuxDataPlaneApplier
from .events import MipEventLog
from .messages import DEFAULT_PORT
from .runtime import RealClock

BACKEND_DIR = Path(__file__).resolve().parents[2]
HOME_NET = "10.10.100.0/24"


class DemoError(NetemError):
    pass


@dataclass(frozen=True)
class DemoSpec:
    scenario_id: str = "mip-live-demo"
    duration_s: float = 20.0
    rate_pps: float = 50.0
    size: int = 64
    lifetime_s: int = 30
    initial_iface: str = "eth-a"
    moves: tuple = ((8.0, "eth-b"), (14.0, "eth-a"))
    probe_port: int = 5005
    make_before_break: bool = False        # bring the new link up and register BEFORE releasing the old one
    old_link_hold_ms: float = 100.0        # MBB: how long the old link stays up after registration


def _check_path(p):
    if not re.fullmatch(r"^/[A-Za-z0-9_./-]+$", str(p)):
        raise DemoError(f"path {p} must be absolute and use only [A-Za-z0-9_./-]")


def run_demo(spec: DemoSpec, mgr, out_dir: Path, *, run_id: str | None = None, python: str | None = None,
             startup_allowance_s: float = 2.0, poll_s: float = 0.02, reg_timeout_s: float = 10.0):
    runner, cfg = mgr.runner, mgr.cfg
    run_id = validate_id(run_id or new_run_id(), "run_id")
    out_dir, python = Path(out_dir).resolve(), python or sys.executable
    for p in (out_dir, Path(python), BACKEND_DIR):
        _check_path(p)
    run_dir = out_dir / spec.scenario_id / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    lcfg = dp.LinuxMipConfig(prefix=mgr.prefix)
    mn_if = {e.ifname: e.address.ip for _l, e in cfg.interfaces_of(lcfg.mn_node)}
    for _, ifn in spec.moves:
        if ifn not in mn_if:
            raise DemoError(f"unknown mobile-node interface {ifn!r}; known {sorted(mn_if)}")
    uid, gid = os.getuid(), os.getgid()

    key = run_dir / "mip.key"
    key.write_text(secrets.token_hex(32) + "\n")
    key.chmod(0o600)
    f = lambda n: run_dir / n
    ctl, status = f("mn.control.jsonl"), f("mn.status.json")
    ctl.write_text("")
    (run_dir / "scenario.json").write_text(json.dumps({
        "schema_version": 1, "scenario_id": spec.scenario_id, "run_id": run_id, "mode": "live",
        "clock": clock.clock_info(), "spec": spec.__dict__, "home_address": lcfg.home_address,
        "limitations": "events and packets share one CLOCK_MONOTONIC only on this host; tunnel overhead analytic"},
        indent=2, default=str))
    (run_dir / "mip_summary.json").write_text(json.dumps({
        "schema_version": 1, "mode": "live", "home_address": lcfg.home_address, "agent_address": lcfg.agent_address,
        "tunnel_overhead_basis": "analytic", "probe_payload_bytes": spec.size, "moves": [list(m) for m in spec.moves]}))
    traffic_ev, mip_ev = EventLog(run_dir), MipEventLog(f("mip_events.orch.csv"), RealClock)
    traffic_ev.record("scenario", "scenario_start", spec.scenario_id)

    applier = LinuxDataPlaneApplier(runner, lcfg, [str(a) for a in mn_if.values()])
    lock = threading.Lock()

    def apply(op, params):
        with lock:
            applier.apply(op, params)

    ctrls = [dp.DataPlaneController(f("ha.dp.req.jsonl"), f("ha.dp.ack.jsonl"), apply),
             dp.DataPlaneController(f("mn.dp.req.jsonl"), f("mn.dp.ack.jsonl"), apply)]
    stops = {n: f(f"{n}.stop") for n in ("ha", "mn", "rx", "tx")}
    procs, logs, downed, failure = {}, [], [], None

    def launch(name, ns, module, args):
        log = open(f(f"{name}.log"), "w")
        logs.append(log)
        cmd = netplan.launch_cmd(ns, uid, gid, python, str(BACKEND_DIR), module, args)
        procs[name] = runner.popen(cmd, stdout=log, stderr=log)

    def wait_file(path, what, timeout=10.0, proc=None):
        end = time.monotonic() + timeout
        while not Path(path).exists():
            if (proc and proc.poll() is not None) or time.monotonic() > end:
                raise DemoError(f"{what} did not become ready (see logs)")
            time.sleep(poll_s)

    def mn_state(timeout, want, coa=None):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                s = json.loads(status.read_text())
                if s["state"] == want and (coa is None or s["care_of"] == str(coa)):
                    return True
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(poll_s)
        return False

    def control(op, coa=None):
        with open(ctl, "a") as fh:
            fh.write(json.dumps({"op": op, **({"coa": str(coa)} if coa else {})}) + "\n")

    common = ["--scenario-id", spec.scenario_id, "--run-id", run_id, "--out-dir", str(out_dir)]
    keyargs = ["--key-file", str(key), "--home-address", lcfg.home_address, "--agent-address", lcfg.agent_address,
               "--port", str(DEFAULT_PORT)]
    try:
        for c in dp.linux_setup_home(lcfg):
            runner.run(c)
        for ifn in mn_if:                                  # MN starts attached to a single foreign network
            if ifn != spec.initial_iface:
                mgr.set_interface(lcfg.mn_node, ifn, False)
                downed.append(ifn)
        for c in ctrls:
            c.start()
        launch("ha", mgr.ns(lcfg.ha_node), "app.mobileip.runtime",
               ["ha", *common, *keyargs, "--bind", lcfg.agent_address, "--home-network", HOME_NET,
                "--stop-file", str(stops["ha"]), "--ready-file", str(f("ha.ready")),
                "--dp-req", str(f("ha.dp.req.jsonl")), "--dp-ack", str(f("ha.dp.ack.jsonl"))])
        wait_file(f("ha.ready"), "home agent", proc=procs["ha"])
        launch("mn", mgr.ns(lcfg.mn_node), "app.mobileip.runtime",
               ["mn", *common, *keyargs, "--control-file", str(ctl), "--status-file", str(status),
                "--lifetime", str(spec.lifetime_s), "--stop-file", str(stops["mn"]),
                "--ready-file", str(f("mn.ready")),
                "--dp-req", str(f("mn.dp.req.jsonl")), "--dp-ack", str(f("mn.dp.ack.jsonl"))])
        wait_file(f("mn.ready"), "mobile node agent", proc=procs["mn"])
        launch("rx", mgr.ns(lcfg.mn_node), "app.traffic.receiver",
               [*common, "--bind", lcfg.home_address, "--port", str(spec.probe_port), "--ready-file", str(f("rx.ready")),
                "--stop-file", str(stops["rx"]), "--idle-timeout", "3"])
        wait_file(f("rx.ready"), "probe receiver", proc=procs["rx"])

        control("attach", mn_if[spec.initial_iface])
        if not mn_state(reg_timeout_s, "registered", mn_if[spec.initial_iface]):
            raise DemoError("initial registration did not complete")

        t0 = clock.mono_ns() + int(startup_allowance_s * 1e9)
        launch("tx", mgr.ns("cn"), "app.traffic.sender",
               [f"{lcfg.home_address}:{spec.probe_port}", *common, "--rate", f"{spec.rate_pps:g}",
                "--duration", f"{spec.duration_s:g}", "--size", str(spec.size), "--drain", "1",
                "--start-at-mono-ns", str(t0), "--stop-file", str(stops["tx"])])
        current = spec.initial_iface
        for i, (at, ifn) in enumerate(spec.moves, 1):
            while clock.mono_ns() < t0 + int(at * 1e9):
                if procs["tx"].poll() not in (None, 0):
                    raise DemoError("probe sender failed (see tx.log)")
                time.sleep(poll_s)
            eid = f"handoff-{i}"
            traffic_ev.record(eid, "handoff_start", f"{current} -> {ifn}")
            mip_ev.emit("mn", "link_change", lcfg.home_address, mn_if[ifn], detail=f"link {ifn} up, {current} down")
            mgr.set_interface(lcfg.mn_node, ifn, True)
            old_if, current = current, ifn
            if not spec.make_before_break:                 # break-before-make: the old link goes away with the movement
                mgr.set_interface(lcfg.mn_node, old_if, False)
                downed = [d for d in downed if d != ifn] + ([old_if] if old_if not in downed else [])
            control("attach", mn_if[ifn])
            ok = mn_state(reg_timeout_s, "registered", mn_if[ifn])
            traffic_ev.record(eid, "handoff_end", "registered" if ok else "registration failed")
            if not ok:
                raise DemoError(f"registration on {ifn} did not complete")
            if spec.make_before_break:                     # release the old link only after registration (+ drain time)
                time.sleep(spec.old_link_hold_ms / 1000.0)
                mgr.set_interface(lcfg.mn_node, old_if, False)
                downed = [d for d in downed if d != ifn] + ([old_if] if old_if not in downed else [])

        end = time.monotonic() + spec.duration_s + 20
        while procs["tx"].poll() is None and time.monotonic() < end:
            time.sleep(poll_s)
        if procs["tx"].poll() is None:
            stops["tx"].write_text("stop\n")
        control("deregister")
        mn_state(reg_timeout_s, "detached")
    except BaseException as e:
        failure = f"{type(e).__name__}: {e}"
    finally:
        for n in ("mn", "rx", "tx"):
            stops[n].write_text("stop\n")
        time.sleep(0.3)
        stops["ha"].write_text("stop\n")                   # HA shutdown removes bindings; controllers must still run
        for n in ("mn", "rx", "tx", "ha"):
            p = procs.get(n)
            end = time.monotonic() + 8
            while p is not None and p.poll() is None and time.monotonic() < end:
                time.sleep(poll_s)
        for c in ctrls:
            c.stop()
        applier.cleanup()
        for c in dp.linux_teardown_home(lcfg):
            try:
                runner.run(c, check=False)
            except NetemError:
                pass
        for ifn in mn_if:                                  # leave the lab as we found it (all MN links up)
            try:
                mgr.set_interface(lcfg.mn_node, ifn, True)
            except NetemError:
                pass
        traffic_ev.record("scenario", "scenario_end", failure or "ok")
        traffic_ev.close()
        mip_ev.close()
        for lg in logs:
            lg.close()
    metrics = analyze_mip_run(run_dir)
    if failure:
        raise DemoError(f"{failure}; partial data kept in {run_dir}")
    return run_dir, metrics
