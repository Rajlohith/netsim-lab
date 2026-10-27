"""python -m app.mobileip sim|analyze|demo"""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from ..netem.errors import NetemError
from .analyze import analyze_mip_run, summary_text


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.mobileip")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sim", help="run the emulated handoff scenario (no root, no network)")
    s.add_argument("--out-dir", type=Path, default=Path("data/results"))
    s.add_argument("--scenario-id", default="mip-handoff")
    s.add_argument("--run-id", default="sim-1")
    s.add_argument("--duration", type=float, default=10.0)
    a = sub.add_parser("analyze", help="metrics for a run directory")
    a.add_argument("run_dir", type=Path)
    d = sub.add_parser("demo", help="live demo in the namespace testbed (sudo)")
    d.add_argument("--out-dir", type=Path, default=Path("data/results"))
    d.add_argument("--prefix", default="nsl-")
    d.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.cmd == "sim":
            from .mobility_sim import MobilitySim, MobilitySpec
            moves = tuple((t, i) for t, i in ((0.4 * args.duration, "eth-b"), (0.7 * args.duration, "eth-a")))
            run = MobilitySim(MobilitySpec(scenario_id=args.scenario_id, run_id=args.run_id,
                                           duration_s=args.duration, moves=moves), args.out_dir).run()
            print(summary_text(analyze_mip_run(run)))
            print(f"raw data + metrics in {run}")
        elif args.cmd == "analyze":
            print(summary_text(analyze_mip_run(args.run_dir)))
        else:
            return _demo(args)
        return 0
    except (NetemError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _demo(args) -> int:
    from ..netem.cli import DEFAULT_CONFIG
    from ..netem.runner import Runner
    from ..netem.topology import TopologyManager
    from ..netem.topology_config import load_config
    from . import dataplane as dp
    from .demo import DemoSpec, run_demo
    if os.geteuid() == 0:
        print("Refusing to run as root; run as a normal user (sudo -n is used per command).", file=sys.stderr)
        return 2
    cfg = load_config(DEFAULT_CONFIG)
    runner = Runner(args.prefix, dry_run=not args.execute)
    mgr = TopologyManager(cfg, runner, args.prefix, None)
    if not args.execute:
        lc = dp.LinuxMipConfig(prefix=args.prefix)
        print("[dry-run] commands the demo would run (privileged ones via sudo -n):")
        for group in (dp.linux_setup_home(lc), dp.linux_mn_set_care_of(lc, "10.10.4.2", False),
                      dp.linux_ha_install(lc, "10.10.4.2", False), dp.linux_ha_install(lc, "10.10.5.2", True),
                      dp.linux_mn_set_care_of(lc, "10.10.5.2", True), dp.linux_ha_remove(lc),
                      dp.linux_mn_clear(lc), dp.linux_teardown_home(lc)):
            for c in group:
                print("  sudo " + shlex.join(c.argv) + f"    # {c.desc}")
        return 0
    run_dir, metrics = run_demo(DemoSpec(), mgr, args.out_dir)
    print(summary_text(metrics))
    print(f"raw data + metrics in {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
