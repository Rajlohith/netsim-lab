"""python -m app.traffic analyze RUN_DIR [...]   |   python -m app.traffic scenario SPEC.yaml"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analyze import analyze_run, summary_text


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.traffic")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="compute metrics.json for run directories")
    a.add_argument("run_dirs", nargs="+", type=Path)
    a.add_argument("--bin", type=float, default=1.0, dest="bin_s", help="throughput bin (s)")
    a.add_argument("--handoff-margin-ms", type=float, default=250.0)
    a.add_argument("--session-id", type=int, default=None)
    a.add_argument("--no-write", action="store_true")
    a.add_argument("--json", action="store_true", help="print full metrics JSON")
    s = sub.add_parser("scenario", help="run a handoff scenario inside the netns topology (needs sudo)")
    s.add_argument("spec", type=Path)
    s.add_argument("--topology", type=Path, default=None)
    s.add_argument("--out-dir", type=Path, default=Path("data/results"))
    s.add_argument("--prefix", default="nsl-")
    s.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "analyze":
        import json
        rc = 0
        for d in args.run_dirs:
            try:
                m = analyze_run(d, session_id=args.session_id, bin_s=args.bin_s,
                                handoff_margin_ms=args.handoff_margin_ms, write=not args.no_write)
            except (FileNotFoundError, ValueError) as e:
                print(f"error: {e}", file=sys.stderr)
                rc = 1
                continue
            print(json.dumps(m, indent=2, sort_keys=True) if args.json else summary_text(m))
        return rc
    from .scenario import scenario_main
    return scenario_main(args)


if __name__ == "__main__":
    sys.exit(main())
