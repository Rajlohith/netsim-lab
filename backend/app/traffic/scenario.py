"""Run a handoff scenario in the netns topology: continuous probes while an interface is flipped.

Orchestration only (measurement code stays in sender/receiver/metrics). Same-host assumption: the
orchestrator, sender and receiver all use this kernel's CLOCK_MONOTONIC, which is what makes
`events.csv` timestamps comparable with packet timestamps (checked through boot_id).
Needs the Stage 2 topology plus sudo; it cannot run in a sandbox without `ip`.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..netem import plan as netplan
from ..netem.errors import NetemError
from . import clock
from .analyze import analyze_run, summary_text
from .recorder import EventLog
from .schema import new_run_id, validate_id

BACKEND_DIR = Path(__file__).resolve().parents[2]


class ScenarioError(NetemError):
    pass


@dataclass(frozen=True)
class HandoffAction:
    at_s: float
    node: str
    ifname: str
    state: str          # "up" | "down"


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    source: str = "mn"
    destination: str = "cn"
    rate_pps: float = 100.0
    duration_s: float = 20.0
    size: int = 64
    port: int = 5005
    drain_s: float = 1.0
    handoffs: tuple = field(default_factory=tuple)


def load_scenario(path) -> ScenarioSpec:
    d = yaml.safe_load(Path(path).read_text())
    allowed = {"scenario_id", "source", "destination", "traffic", "handoffs"}
    if not isinstance(d, dict) or set(d) - allowed:
        raise ScenarioError(f"scenario: unknown/invalid top-level fields {sorted(set(d or {}) - allowed)}")
    traffic = d.get("traffic", {})
    if set(traffic) - {"rate_pps", "duration_s", "size", "port", "drain_s"}:
        raise ScenarioError("scenario.traffic: unknown fields")
    hs = []
    for i, h in enumerate(d.get("handoffs") or []):
        if set(h) != {"at_s", "node", "ifname", "state"} or h["state"] not in ("up", "down"):
            raise ScenarioError(f"handoffs[{i}]: need at_s,node,ifname,state(up|down)")
        hs.append(HandoffAction(float(h["at_s"]), h["node"], h["ifname"], h["state"]))
    try:
        spec = ScenarioSpec(validate_id(d["scenario_id"], "scenario_id"), d.get("source", "mn"),
                            d.get("destination", "cn"), **{k: traffic[k] for k in traffic},
                            handoffs=tuple(sorted(hs, key=lambda h: h.at_s)))
    except (KeyError, ValueError) as e:
        raise ScenarioError(f"scenario: {e}") from None
    if spec.duration_s <= 0 or any(not (0 <= h.at_s <= spec.duration_s) for h in spec.handoffs):
        raise ScenarioError("scenario: handoff times must lie within [0, duration_s]")
    return spec


def _check_path(p: Path):
    if not re.fullmatch(r"^/[A-Za-z0-9_./-]+$", str(p)):
        raise ScenarioError(f"path {p} must be absolute and use only [A-Za-z0-9_./-] (no spaces)")


def run_scenario(spec: ScenarioSpec, mgr, out_dir: Path, *, run_id: str | None = None,
                 python: str | None = None, startup_allowance_s: float = 2.0, poll_s: float = 0.02):
    """Returns (run_dir, metrics). Raises ScenarioError after cleanup if a tool failed."""
    runner = mgr.runner
    run_id = validate_id(run_id or new_run_id(), "run_id")
    out_dir = Path(out_dir).resolve()
    python = python or sys.executable
    for p in (out_dir, Path(python), BACKEND_DIR):
        _check_path(p)
    run_dir = out_dir / spec.scenario_id / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    cfg = mgr.cfg
    dst_ip = str(cfg.addresses_of(spec.destination)[0])
    uid, gid = os.getuid(), os.getgid()
    stop_rx, stop_tx = run_dir / "receiver.stop", run_dir / "sender.stop"
    ready = run_dir / "receiver.ready"

    (run_dir / "scenario.json").write_text(json.dumps({
        "schema_version": 1, "scenario_id": spec.scenario_id, "run_id": run_id,
        "start_wall_iso": clock.iso_utc(clock.wall_ns()), "clock": clock.clock_info(),
        "topology": cfg.name, "spec": {**spec.__dict__, "handoffs": [h.__dict__ for h in spec.handoffs]},
        "destination_ip": dst_ip,
        "limitations": "events.csv/packet timestamps share one CLOCK_MONOTONIC only on this host",
    }, indent=2, default=str))
    events = EventLog(run_dir)
    events.record("scenario", "scenario_start", spec.scenario_id)

    common = ["--scenario-id", spec.scenario_id, "--run-id", run_id, "--out-dir", str(out_dir)]
    rx_cmd = netplan.launch_cmd(mgr.ns(spec.destination), uid, gid, python, str(BACKEND_DIR),
                                "app.traffic.receiver",
                                common + ["--bind", dst_ip, "--port", str(spec.port), "--ready-file", str(ready),
                                          "--stop-file", str(stop_rx), "--idle-timeout", "3"])
    procs, brought_down, failure = {}, [], None
    logs = []
    try:
        logs = [open(run_dir / "receiver.log", "w"), open(run_dir / "sender.log", "w")]
        procs["receiver"] = runner.popen(rx_cmd, stdout=logs[0], stderr=logs[0])
        deadline = time.monotonic() + 10
        while not ready.exists():
            if procs["receiver"].poll() is not None or time.monotonic() > deadline:
                raise ScenarioError("receiver did not become ready (see receiver.log)")
            time.sleep(poll_s)

        t0 = clock.mono_ns() + int(startup_allowance_s * 1e9)
        tx_cmd = netplan.launch_cmd(
            mgr.ns(spec.source), uid, gid, python, str(BACKEND_DIR), "app.traffic.sender",
            [f"{dst_ip}:{spec.port}"] + common + ["--rate", f"{spec.rate_pps:g}", "--duration", f"{spec.duration_s:g}",
                                                 "--size", str(spec.size), "--drain", f"{spec.drain_s:g}",
                                                 "--start-at-mono-ns", str(t0), "--stop-file", str(stop_tx)])
        procs["sender"] = runner.popen(tx_cmd, stdout=logs[1], stderr=logs[1])

        for i, h in enumerate(spec.handoffs, 1):
            while clock.mono_ns() < t0 + int(h.at_s * 1e9):
                if procs["sender"].poll() not in (None, 0):
                    raise ScenarioError("sender exited with an error (see sender.log)")
                time.sleep(poll_s)
            eid = f"handoff-{i}"
            detail = f"{h.node}:{h.ifname} {h.state}"
            events.record(eid, "handoff_start", detail)
            mgr.set_interface(h.node, h.ifname, h.state == "up")
            if h.state == "down":
                brought_down.append((h.node, h.ifname))
            elif (h.node, h.ifname) in brought_down:
                brought_down.remove((h.node, h.ifname))
            events.record(eid, "handoff_end", detail)

        end = time.monotonic() + spec.duration_s + spec.drain_s + 15
        while procs["sender"].poll() is None and time.monotonic() < end:
            time.sleep(poll_s)
        if procs["sender"].poll() is None:
            stop_tx.write_text("stop\n")                       # ask for a clean finish
            time.sleep(spec.drain_s + 2)
        if procs["sender"].returncode not in (0, None):
            failure = "sender failed (see sender.log)"
        end = time.monotonic() + 8
        while procs["receiver"].poll() is None and time.monotonic() < end:
            time.sleep(poll_s)
        if procs["receiver"].poll() is None:
            stop_rx.write_text("stop\n")
            time.sleep(1.0)
    except BaseException as e:
        failure = f"{type(e).__name__}: {e}"
        for f in (stop_tx, stop_rx):
            f.write_text("stop\n")
        time.sleep(0.5)
    finally:
        for node, ifname in brought_down:                      # leave the lab as we found it
            try:
                mgr.set_interface(node, ifname, True)
            except NetemError:
                pass
        events.record("scenario", "scenario_end", failure or "ok")
        events.close()
        for f in logs:
            f.close()
    metrics = analyze_run(run_dir)
    if failure:
        raise ScenarioError(f"{failure}; partial data kept in {run_dir}")
    return run_dir, metrics


def scenario_main(args) -> int:
    from ..netem.cli import DEFAULT_CONFIG
    from ..netem.runner import Runner
    from ..netem.topology import TopologyManager
    from ..netem.topology_config import load_config
    if os.geteuid() == 0:
        print("Refusing to run as root; run as a normal user (sudo -n is used per command).", file=sys.stderr)
        return 2
    try:
        spec = load_scenario(args.spec)
        cfg = load_config(args.topology or DEFAULT_CONFIG)
        runner = Runner(args.prefix, dry_run=not args.execute)
        mgr = TopologyManager(cfg, runner, args.prefix, None)
        if not args.execute:
            print(f"[dry-run] scenario {spec.scenario_id}: handoffs " +
                  ", ".join(f"t+{h.at_s:g}s {h.node}:{h.ifname} {h.state}" for h in spec.handoffs))
            for h in spec.handoffs:
                mgr.set_interface(h.node, h.ifname, h.state == "up")
            for c in runner.history:
                print("[dry-run] " + shlex.join(c))
            return 0
        run_dir, metrics = run_scenario(spec, mgr, args.out_dir)
        print(summary_text(metrics))
        print(f"raw data + metrics.json in {run_dir}")
        return 0
    except (NetemError, OSError, ValueError, yaml.YAMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
