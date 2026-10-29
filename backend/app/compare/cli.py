"""python -m app.compare list | run | matrix"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .approaches import APPROACHES, SUPPORT
from .experiment import ENVIRONMENTS, ExperimentSpec
from ..netem.conditions import NetworkConditions
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
    r.add_argument("--seed", type=int, default=0, help="Stage 7: seed of the random streams (loss) and recorded with the run")
    r.add_argument("--latency-scale", type=float, default=1.0, help="Stage 7: multiply every link delay")
    r.add_argument("--loss-percent", type=float, default=0.0, help="Stage 7: random loss on the access links (both directions)")
    r.add_argument("--ha-distance-ms", type=float, default=0.0, help="Stage 7: extra one-way delay on every link touching the Home Agent")
    r.add_argument("--registration-loss-percent", type=float, default=0.0, help="Stage 7: loss of Mobile IP registration messages")
    r.add_argument("--ha-processing-ms", type=float, default=1.0, help="Stage 7: Home Agent registration processing time")
    r.add_argument("--detect-delay-ms", type=float, default=20.0, help="movement-detection delay")
    r.add_argument("--execute", action="store_true", help="live environment: really run privileged commands")
    r.add_argument("--predict-model", type=Path, default=None, metavar="MODEL_DIR",
                   help="Stage 6 (opt-in): after the run, write handoff_predictions.csv/json into the run directory using this model bundle")
    sub.choices["list"].add_argument("--json", action="store_true", help="machine-readable approach/configuration/support listing")
    pl = sub.add_parser("plan", help="Stage 7: show the design of a campaign (conditions, approach configurations, run count) without running")
    pl.add_argument("--config", type=Path, required=True)
    c = sub.add_parser("campaign", help="Stage 7: run (or resume) a replicated campaign, then analyse and plot it")
    c.add_argument("--config", type=Path, required=True)
    c.add_argument("--out-dir", type=Path, required=True)
    c.add_argument("--predict-model", type=Path, default=None, metavar="MODEL_DIR", help="attach Stage 6 predictions to every finished run")
    c.add_argument("--retry-failed", action="store_true", help="also rerun runs recorded as failed")
    c.add_argument("--max-runs", type=int, default=None, help="stop cleanly after this many NEW runs (resume later with the same command)")
    c.add_argument("--no-analyze", action="store_true")
    an = sub.add_parser("analyze", help="Stage 7: recompute statistics/exports/report from saved results (no experiment is run)")
    an.add_argument("results_dir", type=Path)
    an.add_argument("--out-dir", type=Path, default=None)
    pt = sub.add_parser("plots", help="Stage 7: draw charts from the saved analysis outputs")
    pt.add_argument("results_dir", type=Path)
    rp = sub.add_parser("reproduce", help="Stage 7: re-run saved runs from plan.json and check they are byte-identical")
    rp.add_argument("results_dir", type=Path)
    rp.add_argument("--sample", type=int, default=10, help="number of runs to re-run (spread over the campaign); 0 = all")
    ss = sub.add_parser("sample-size", help="Stage 7: replicates needed for a mean's CI half-width, from a pilot standard deviation (derived estimate)")
    ss.add_argument("--sd", type=float, required=True)
    ss.add_argument("--halfwidth", type=float, required=True)
    ss.add_argument("--conf", type=float, default=0.95)
    m = sub.add_parser("matrix", help="run the standard comparison set and write comparison.md/json")
    m.add_argument("--out-dir", type=Path, default=Path("data/results/comparison"))
    m.add_argument("--duration", type=float, default=10.0)
    a = p.parse_args(argv)

    if a.cmd == "list" and a.json:
        from .approaches import approach_configs
        print(json.dumps({"aioquic_installed": quic_available(),
                          "approaches": {k: {"name": i.name, "support": {e: list(SUPPORT[k][e]) for e in ENVIRONMENTS}} for k, i in APPROACHES.items()},
                          "configurations": [{"id": c, "approach": ap, "variant": v, "overrides": o} for c, ap, v, o in approach_configs()]},
                         indent=2, sort_keys=True))
        return 0
    if a.cmd == "plan":
        from .campaign import CampaignConfig, build_plan
        plan = build_plan(CampaignConfig.load(a.config))
        print(json.dumps(plan.summary(), indent=2))
        print("conditions:", ", ".join(c.cid for c in plan.conditions))
        for c in plan.cells:
            print(f"  not executed: {c.condition} / {c.config_id}: {c.status}: {c.reason}")
        return 0
    if a.cmd == "campaign":
        from .analysis import analyze
        from .campaign import CampaignConfig, execute
        from .plots import make_plots
        cfg = CampaignConfig.load(a.config)
        res = execute(cfg, a.out_dir, retry_failed=a.retry_failed, predict_model=a.predict_model, max_runs=a.max_runs,
                      progress=lambda i, n, key, st: print(f"[{i}/{n}] {key}: {st}", flush=True) if (i % 25 == 0 or st != "ok") else None)
        print(json.dumps(res, indent=2))
        if res["interrupted"]:
            print("campaign interrupted: the interrupted run is recorded; rerun the same command to resume", file=sys.stderr)
            return 130
        if not a.no_analyze:
            print(json.dumps(analyze(a.out_dir), indent=2))
            print("charts:", len(make_plots(a.out_dir)))
        return 0
    if a.cmd == "analyze":
        from .analysis import analyze
        print(json.dumps(analyze(a.results_dir, a.out_dir), indent=2))
        return 0
    if a.cmd == "plots":
        from .plots import make_plots
        for p in make_plots(a.results_dir):
            print(p)
        return 0
    if a.cmd == "sample-size":
        from .stats import required_n
        n = required_n(a.sd, a.halfwidth, a.conf)
        print(f"n >= {n} replicates for a {a.conf:.0%} t-interval half-width <= {a.halfwidth:g} with pilot sd {a.sd:g} "
              "(derived estimate; the pilot sd is itself uncertain)")
        return 0
    if a.cmd == "reproduce":
        from .reproduce import reproduce
        rep = reproduce(a.results_dir, None if a.sample == 0 else a.sample)
        print(json.dumps(rep, indent=2))
        return 0 if not rep["mismatches"] else 1
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
                              tcp_detect=a.tcp_detect, old_link_hold_ms=a.hold_ms, variant=a.variant, seed=a.seed,
                              detect_delay_ms=a.detect_delay_ms,
                              conditions=NetworkConditions(latency_scale=a.latency_scale, loss_percent=a.loss_percent,
                                                           ha_distance_ms=a.ha_distance_ms, ha_processing_ms=a.ha_processing_ms,
                                                           registration_loss_percent=a.registration_loss_percent, seed=a.seed))
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
