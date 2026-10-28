"""python -m app.compare list | run | matrix"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .approaches import APPROACHES, SUPPORT
from .experiment import ENVIRONMENTS, ExperimentSpec
from .matrix import run_matrix
from .quic_sim import quic_available
from .runner import LiveContext, check, run_experiment


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.compare")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="approaches, where they can run, and whether they are available here")
    r = sub.add_parser("run", help="run ONE experiment through the common interface")
    r.add_argument("--approach", required=True)
    r.add_argument("--environment", default="emulated", choices=ENVIRONMENTS)
    r.add_argument("--handoff-mode", default="bbm", choices=("bbm", "mbb"))
    r.add_argument("--out-dir", type=Path, default=Path("data/results"))
    r.add_argument("--run-id", default="run-1")
    r.add_argument("--duration", type=float, default=10.0)
    r.add_argument("--tcp-detect", default="event", choices=("event", "timeout"))
    r.add_argument("--hold-ms", type=float, default=100.0, help="make-before-break old-link hold")
    r.add_argument("--variant", default="")
    r.add_argument("--execute", action="store_true", help="live environment: really run privileged commands")
    r.add_argument("--predict-model", type=Path, default=None, metavar="MODEL_DIR",
                   help="Stage 6 (opt-in): after the run, write handoff_predictions.csv/json into the run directory using this model bundle")
    m = sub.add_parser("matrix", help="run the standard comparison set and write comparison.md/json")
    m.add_argument("--out-dir", type=Path, default=Path("data/results/comparison"))
    m.add_argument("--duration", type=float, default=10.0)
    a = p.parse_args(argv)

    if a.cmd == "list":
        print(f"aioquic installed: {quic_available()}")
        for k, info in APPROACHES.items():
            print(f"\n{k}: {info.name}\n  {info.status}")
            for env in ENVIRONMENTS:
                st, why = SUPPORT[k][env]
                print(f"  {env:9} {st:11} {why}")
        return 0
    if a.cmd == "run":
        spec = ExperimentSpec(a.approach, a.environment, run_id=a.run_id, duration_s=a.duration, handoff_mode=a.handoff_mode,
                              tcp_detect=a.tcp_detect, old_link_hold_ms=a.hold_ms, variant=a.variant)
        live = None
        if a.environment == "live":
            st, why = check(spec)
            if st == "ok" and not a.execute:
                print("live environment: dry-run only. Add --execute (needs the Stage 2 lab, sudo -v, ip/ipip).")
                return 0
            if st == "ok":
                if os.geteuid() == 0:
                    print("Refusing to run as root.", file=sys.stderr)
                    return 2
                from ..netem.cli import DEFAULT_CONFIG
                from ..netem.runner import Runner
                from ..netem.topology import TopologyManager
                from ..netem.topology_config import load_config
                live = LiveContext(TopologyManager(load_config(DEFAULT_CONFIG), Runner("nsl-", dry_run=False), "nsl-", None))
        res = run_experiment(spec, a.out_dir, live)
        print(f"status: {res.status}" + (f"\nreason: {res.reason}" if res.reason else ""))
        if res.metrics:
            print(json.dumps({k: res.metrics[k] for k in ("handoff_summary", "loss", "mobility_overhead", "continuity")}, indent=2))
            print(f"run directory: {res.run_dir}")
        if a.predict_model is not None and res.status == "ok" and res.run_dir:
            from ..ml.integration import annotate_run      # lazy: the ML stack is only needed when asked for
            summ = annotate_run(res.run_dir, a.predict_model)
            print(f"predictions: {summ['decision_points']} decision points, {summ['alarms']} alarms -> {res.run_dir}/handoff_predictions.csv")
        return 0 if res.status in ("ok", "unsupported", "unavailable") else 1
    base = ExperimentSpec("tcp-reconnect", duration_s=a.duration)
    results, path = run_matrix(a.out_dir, base)
    print(Path(path).read_text())
    print(f"written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
