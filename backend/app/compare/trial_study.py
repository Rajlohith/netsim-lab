"""Trial-count study: how many replicate runs ("trials") are needed before an aggregated number can be trusted?
(SIMULATOR-GENERATED analysis.)

Method (no assumption about the distribution): run a LARGE number of replicates (default 60) of one cell, then draw many random
subsets of size n WITHOUT replacement and look at how the aggregated value (the median across trials) behaves:
  * ci_width      : width of the exact 95 % distribution-free CI of the median for the first n replicates (stats.median_ci_exact)
  * within_tol    : fraction of random size-n subsets whose median lies within +-tol of the full-sample median
  * n_needed      : smallest n with within_tol >= target (default 95 % of subsets within +-10 %)
and, for comparison, the classical t-based sample size `stats.required_n(sd, halfwidth)` from a pilot of 12.
Aggregation rule used everywhere in this project: (1) handoffs inside one run are averaged FIRST (they share the run's loss draws and
state, so they are not independent trials); (2) the median across runs is reported with its exact CI; the mean is a convenience only
(skewed, heavy-tailed outcomes such as 'a handoff that took a registration retry' pull the mean away from the typical run).

    PYTHONPATH=backend python3 -m app.compare.trial_study run --out-dir data/studies/trials --replicates 60
    PYTHONPATH=backend python3 -m app.compare.trial_study report --out-dir data/studies/trials
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import campaign as C
from .stats import median_ci_exact, required_n


def config(replicates=60, loss=2, base_seed=20261028):
    return C.CampaignConfig.from_json({
        "name": "trials", "base_seed": base_seed, "replicates": replicates, "duration_s": 12.0, "rate_pps": 100.0, "payload": 64,
        "environment": "emulated", "approaches": ["mip-basic", "mip-mbb"],
        "schedule": {"first_move_s": [3.5, 4.5], "dwell_s": 3.0, "dwell_jitter_s": 0.25},
        "sweeps": {"loss_percent": [loss]}})


def convergence(values, tol=0.10, target=0.95, draws=2000, seed=1, ns=(3, 5, 6, 8, 10, 12, 15, 20, 30, 40, 60)):
    v = np.asarray([x for x in values if x is not None and not np.isnan(x)], float)
    full = float(np.median(v))
    rng = np.random.default_rng(seed)
    rows = []
    for n in ns:
        if n > len(v):
            break
        meds = np.array([np.median(rng.choice(v, n, replace=False)) for _ in range(draws)])
        ci = median_ci_exact(v[:n])
        within = float(np.mean(np.abs(meds - full) <= tol * abs(full))) if full else float("nan")
        rows.append({"n": n, "ci_lo": None if ci is None else ci[0], "ci_hi": None if ci is None else ci[1],
                     "within_tol": round(within, 3), "median_spread_p5_p95": [round(float(np.percentile(meds, 5)), 3), round(float(np.percentile(meds, 95)), 3)]})
    need = next((r["n"] for r in rows if r["within_tol"] >= target), None)
    pilot = v[:12]
    sd = float(pilot.std(ddof=1))
    req = required_n(sd, tol * abs(full)) if sd > 0 and full else None
    return {"full_median": full, "n_total": int(len(v)), "tolerance": tol, "target_fraction": target, "n_needed_by_subsampling": need,
            "t_based_required_n_from_pilot12": req, "pilot_sd": round(sd, 4), "table": rows}


def report(out_dir: Path) -> dict:
    import pandas as pd
    rows = pd.read_csv(Path(out_dir) / "analysis" / "runs.csv")
    rows = rows[rows.status == "ok"]
    res = {}
    for (cond, cfg), g in rows.groupby(["condition", "config_id"]):
        for metric in ("app_stall_ms", "loss_percent"):
            res[f"{cond}|{cfg}|{metric}"] = convergence(g.sort_values("replicate")[metric].to_numpy(float))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m app.compare.trial_study")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--out-dir", required=True); r.add_argument("--replicates", type=int, default=60)
    p = sub.add_parser("report"); p.add_argument("--out-dir", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        print(json.dumps(C.execute(config(a.replicates), a.out_dir), indent=1, default=str))
    else:
        print(json.dumps(report(Path(a.out_dir)), indent=1))


if __name__ == "__main__":
    main()
