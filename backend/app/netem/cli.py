"""Command line: python -m app.netem <command>   (see scripts/topology.sh)."""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path

from .errors import ConfigError, NetemError
from .plan import build_create_plan
from .runner import Runner
from .topology import TopologyManager
from .topology_config import NetemSpec, load_config

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "config" / "topology.yaml"


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _common() -> argparse.ArgumentParser:
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="topology YAML")
    c.add_argument("--prefix", default=os.environ.get("NETSIM_NETNS_PREFIX", "nsl-"))
    c.add_argument("--execute", action="store_true",
                   help="really run privileged commands (default: dry-run unless NETSIM_DRY_RUN=false)")
    c.add_argument("--allow-root", action="store_true",
                   help="allow running this Python process as root (not recommended)")
    return c


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="netsim-topology", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = _common()
    sub.add_parser("validate", parents=[c], help="validate the YAML (no privileges)")
    sub.add_parser("plan", parents=[c], help="print the commands `create` would run")
    sub.add_parser("create", parents=[c], help="create namespaces, veth pairs, routes, netem")
    sub.add_parser("status", parents=[c], help="show addresses/routes/forwarding/qdiscs (read-only)")
    sub.add_parser("destroy", parents=[c], help="remove only this project's namespaces")
    pg = sub.add_parser("ping", parents=[c], help="measure RTT/loss from a node to an IP")
    pg.add_argument("src", help="source node key, e.g. mn")
    pg.add_argument("dst", help="destination IPv4 address")
    pg.add_argument("-c", "--count", type=int, default=5)
    pg.add_argument("-i", "--interval", type=float, default=0.2)
    sh = sub.add_parser("shape", parents=[c], help="change netem on a link at runtime")
    sh.add_argument("link")
    sh.add_argument("--end", choices=("a", "b", "both"), default="both")
    sh.add_argument("--delay-ms", type=float, default=0.0)
    sh.add_argument("--jitter-ms", type=float, default=0.0)
    sh.add_argument("--loss", type=float, default=0.0, dest="loss_percent")
    sh.add_argument("--rate-mbit", type=float, default=None)
    sh.add_argument("--clear", action="store_true", help="remove netem from the link")
    return p


def main(argv=None, out=sys.stdout, err=sys.stderr) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.cmd == "validate":
            print(f"OK: {cfg.name}: {len(cfg.nodes)} nodes, {len(cfg.links)} links, "
                  f"{len(cfg.routes)} routes", file=out)
            return 0
        if args.cmd == "plan":
            for c in build_create_plan(cfg, args.prefix):
                print(("sudo " if c.privileged else "") + shlex.join(c.argv), file=out)
            return 0

        if os.geteuid() == 0 and not args.allow_root:
            print("Refusing to run as root: run as a normal user; privileged commands use "
                  "`sudo -n` individually (or pass --allow-root).", file=err)
            return 2
        read_only = args.cmd in ("status", "ping")
        execute = args.execute or read_only or not _env_bool("NETSIM_DRY_RUN", True)
        runner = Runner(args.prefix, dry_run=not execute)
        state = ROOT / os.environ.get("NETSIM_DATA_DIR", "data") / "topology_state.json"
        mgr = TopologyManager(cfg, runner, args.prefix, state)

        if args.cmd == "create":
            created = mgr.create()
            print(f"created {len(created)} namespaces: {', '.join(created)}" if execute else
                  "[dry-run] nothing executed; use --execute", file=out)
        elif args.cmd == "destroy":
            r = mgr.destroy()
            print(f"deleted={r.deleted} absent={r.absent} refused={r.refused}", file=out)
        elif args.cmd == "status":
            print(mgr.status(), file=out)
        elif args.cmd == "ping":
            s = mgr.ping(args.src, args.dst, args.count, args.interval)
            print(f"sent={s.transmitted} recv={s.received} loss={s.loss_percent}% "
                  f"rtt min/avg/max = {s.rtt_min_ms}/{s.rtt_avg_ms}/{s.rtt_max_ms} ms", file=out)
        elif args.cmd == "shape":
            spec = None if args.clear else NetemSpec(args.delay_ms, args.jitter_ms, args.loss_percent,
                                                     args.rate_mbit)
            done = mgr.shape(args.link, args.end, spec)
            print(f"{'cleared' if spec is None or spec.is_empty() else 'shaped'}: {done}", file=out)
        if runner.dry_run:
            for h in runner.history:
                print("[dry-run] " + shlex.join(h), file=out)
        return 0
    except NetemError as e:
        print(f"error: {e}", file=err)
        return 1
