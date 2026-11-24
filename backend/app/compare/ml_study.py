"""ML study: can a model PREDICT handoff loss/stall from the network conditions, and can it CHOOSE the best approach?
(SIMULATOR-GENERATED data; the model describes the emulator under the sampled conditions, not any real network.)

Why this is what ML can honestly add here. Loss in this lab is not random noise to be "denoised": it is rate x blackout, and the
blackout is set by detection delay + signalling round trips + path delays. A model cannot remove it. What a model CAN do is
  (a) PREDICT loss / stall for conditions nobody ran (a surrogate that replaces slow campaigns), and
  (b) SELECT the approach with the smallest predicted stall for the current conditions (an adaptive policy, evaluated by its
      REGRET against the true best approach on conditions the model never saw).
Evaluation rules: grouped cross-validation (all runs of one condition vector stay in the same fold, so the score measures
generalisation to UNSEEN conditions), comparison against a trivial baseline (predict the training mean) and against the
analytic blackout formula, and the number of samples is stated.

    PYTHONPATH=backend python3 -m app.compare.ml_study generate --out data/studies/ml_dataset.csv --n-conditions 120
    PYTHONPATH=backend python3 -m app.compare.ml_study evaluate --data data/studies/ml_dataset.csv
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

import numpy as np

from ..netem.conditions import NetworkConditions
from .approaches import approach_configs
from .experiment import ExperimentSpec
from .runner import run_experiment

FEATURES = ["latency_scale", "loss_percent", "ha_distance_ms", "detect_delay_ms", "registration_loss_percent", "ha_processing_ms"]
# sampling ranges: wide enough to cover the example/standard sweeps (see config/experiments/*.yaml); the BASELINE point is not special
RANGES = {"latency_scale": (0.5, 4.0), "loss_percent": (0.0, 5.0), "ha_distance_ms": (0.0, 200.0), "detect_delay_ms": (0.0, 250.0),
          "registration_loss_percent": (0.0, 50.0), "ha_processing_ms": (1.0, 200.0)}


def sample_conditions(n: int, seed: int) -> list:
    """Latin-hypercube sample: every feature is stratified into n equal slices, so n points cover each range evenly."""
    rng = np.random.default_rng(seed)
    cols = {}
    for f, (lo, hi) in RANGES.items():
        u = (rng.permutation(n) + rng.random(n)) / n
        cols[f] = lo + u * (hi - lo)
    return [{f: round(float(cols[f][i]), 3) for f in FEATURES} for i in range(n)]


def _run_one(approach_cfg, cond: dict, seed: int, tmp: Path) -> dict | None:
    config_id, approach, variant, overrides = approach_cfg
    rng = np.random.default_rng(seed)
    t1 = round(float(rng.uniform(3.5, 4.5)), 3)
    moves = ((t1, "eth-b"), (round(t1 + 3.0 + float(rng.uniform(-0.25, 0.25)), 3), "eth-a"))
    nc = NetworkConditions(latency_scale=cond["latency_scale"], loss_percent=cond["loss_percent"],
                           ha_distance_ms=cond["ha_distance_ms"], registration_loss_percent=cond["registration_loss_percent"],
                           ha_processing_ms=cond["ha_processing_ms"], seed=seed)
    kw = dict(overrides)
    spec = ExperimentSpec(approach=approach, scenario_id=f"{config_id}", run_id=f"s{seed}", duration_s=12.0, moves=moves, seed=seed,
                          variant=variant, conditions=nc, detect_delay_ms=cond["detect_delay_ms"], **kw)
    res = run_experiment(spec, tmp)
    if res.status != "ok":
        return None
    m = json.loads((tmp / spec.scenario_id / spec.run_id / "comparison_metrics.json").read_text())
    hs = m.get("handoffs") or []
    stalls = [h["service_gap_ms"] for h in hs if h.get("service_gap_ms") is not None]
    return {"config_id": config_id, **cond, "seed": seed, "loss_percent_out": (m.get("loss") or {}).get("percent"),
            "app_stall_ms": float(np.mean(stalls)) if stalls else None,
            "unrecovered": sum(1 for h in hs if not h.get("recovered"))}


def generate(out: Path, n_conditions: int, seed: int):
    import csv
    configs = approach_configs()
    conds = sample_conditions(n_conditions, seed)
    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="mlds-"))
    try:
        for i, c in enumerate(conds):
            for cfg in configs:
                if cfg[1] == "quic-migration":
                    continue                              # needs aioquic; reported as unavailable elsewhere, never faked
                r = _run_one(cfg, c, seed * 1000 + i, tmp)
                if r:
                    r["cond_id"] = i
                    rows.append(r)
                shutil.rmtree(tmp / cfg[0], ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def evaluate(data: Path, folds: int = 5, seed: int = 0) -> dict:
    import pandas as pd
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error, r2_score
    from sklearn.model_selection import GroupKFold

    df = pd.read_csv(data).dropna(subset=["app_stall_ms", "loss_percent_out"])
    cfgs = sorted(df.config_id.unique())
    X = pd.get_dummies(df[FEATURES + ["config_id"]], columns=["config_id"]).astype(float)
    groups = df.cond_id.to_numpy()
    out = {"n_rows": int(len(df)), "n_conditions": int(df.cond_id.nunique()), "configs": cfgs, "folds": folds}
    preds = {}
    for target, tf in (("loss_percent_out", lambda y: y), ("app_stall_ms", np.log1p)):
        y = df[target].to_numpy(float)
        yt = tf(y)
        p = np.zeros(len(df)); base = np.zeros(len(df))
        for tr, te in GroupKFold(n_splits=folds).split(X, yt, groups):
            m = GradientBoostingRegressor(n_estimators=300, max_depth=3, learning_rate=0.05, subsample=0.8, random_state=seed)
            m.fit(X.iloc[tr], yt[tr])
            p[te] = m.predict(X.iloc[te])
            base[te] = yt[tr].mean()
        back = (lambda v: v) if target == "loss_percent_out" else np.expm1
        pr, bs = np.clip(back(p), 0, None), back(base)
        preds[target] = pr
        out[target] = {"R2_model": round(float(r2_score(y, pr)), 3), "MAE_model": round(float(mean_absolute_error(y, pr)), 3),
                       "MAE_predict_training_mean": round(float(mean_absolute_error(y, bs)), 3), "unit": "%" if "loss" in target else "ms"}
    # per-approach accuracy (a pooled R2 hides that heavy-tailed approaches are harder to predict)
    per = {}
    for c in cfgs:
        m = (df.config_id == c).to_numpy()
        per[c] = {"stall_MAE_ms": round(float(mean_absolute_error(df.app_stall_ms[m], np.clip(np.expm1(np.log1p(preds["app_stall_ms"][m])), 0, None))), 1),
                  "stall_MAE_predict_mean_ms": round(float(mean_absolute_error(df.app_stall_ms[m], np.full(m.sum(), df.app_stall_ms[m].mean()))), 1),
                  "loss_MAE_pct": round(float(mean_absolute_error(df.loss_percent_out[m], preds["loss_percent_out"][m])), 2),
                  "loss_MAE_predict_mean_pct": round(float(mean_absolute_error(df.loss_percent_out[m], np.full(m.sum(), df.loss_percent_out[m].mean()))), 2)}
    out["per_approach"] = per

    # approach selection: for each unseen condition pick the config with the lowest PREDICTED stall; regret = true stall(chosen) - true stall(best).
    # Two settings: all approaches, and WITHOUT make-before-break (it needs two simultaneously usable links, which is not always available,
    # and it wins on stall in every condition, which makes the first setting trivial).
    df = df.assign(pred_stall=preds["app_stall_ms"])

    def select(allowed):
        regret, hits, fixed = [], 0, {c: [] for c in allowed}
        for _cid, g in df.groupby("cond_id"):
            g = g[g.config_id.isin(allowed)]
            if g.config_id.nunique() < len(allowed):
                continue
            best, pick = g.loc[g.app_stall_ms.idxmin()], g.loc[g.pred_stall.idxmin()]
            regret.append(float(pick.app_stall_ms - best.app_stall_ms)); hits += int(pick.config_id == best.config_id)
            for c in allowed:
                fixed[c].append(float(g[g.config_id == c].app_stall_ms.iloc[0] - best.app_stall_ms))
        return {"n_conditions_scored": len(regret), "exact_best_hit_rate": round(hits / max(len(regret), 1), 3),
                "mean_regret_ms_model": round(float(np.mean(regret)), 1),
                "mean_regret_ms_always_one_approach": {c: round(float(np.mean(v)), 1) for c, v in fixed.items()}}
    out["selection_all_approaches"] = select(cfgs)
    out["selection_without_mbb"] = select([c for c in cfgs if c != "mip-mbb"])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m app.compare.ml_study")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate"); g.add_argument("--out", required=True); g.add_argument("--n-conditions", type=int, default=120); g.add_argument("--seed", type=int, default=7)
    e = sub.add_parser("evaluate"); e.add_argument("--data", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        print("rows written:", generate(Path(a.out), a.n_conditions, a.seed))
    else:
        print(json.dumps(evaluate(Path(a.data)), indent=1))


if __name__ == "__main__":
    main()
