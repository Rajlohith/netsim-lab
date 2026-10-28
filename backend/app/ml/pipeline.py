"""End-to-end: raw runs -> dataset -> grouped split -> fit -> validate threshold -> held-out evaluation -> bundle + results + plots.

If the data is insufficient the pipeline writes results.json with status 'pending' and the reasons; it saves no model and reports no
metric. Nothing is estimated, interpolated or assumed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import evaluate as E
from .bundle import save_bundle, _jd
from .config import MLConfig, MinData, PIPELINE_VERSION, SCHEMA_VERSION
from .dataset import DatasetResult, build_dataset, class_balance, load_dataset, write_dataset
from .features import FEATURE_SETS
from .models import FittedModel, MODEL_LABELS, ThresholdBaseline, fit_all
from .splits import InsufficientDataError, SPLITS, assert_no_leakage, group_split, split_frames

CAVEATS = [
    "All data is SIMULATED (Stage 4/5 discrete-event emulator, virtual time, fixed link delays). Metrics say nothing about real networks.",
    "No feature is physical radio signal strength; the simulator has no radio model.",
    "In the emulator a handoff is an abrupt link change: the measurements contain no gradual pre-handoff degradation. Any skill above the "
    "'always alarm' reference can only come from regularities of the experiment generator (e.g. dwell-time distribution seen through the "
    "handoff-history features), which the measurements-only ablation and the per-scenario breakdown help to expose.",
    "Models are trained on the training split only; the validation split chooses the alarm threshold; the test split is used once for the reported numbers.",
    "Few scenario families are held out; see the bootstrap intervals (resampling whole experiments) for the resulting uncertainty.",
]


def _model_report(name, model: FittedModel, frames: dict, step_s: float, *, with_curves=False) -> dict:
    val, test = frames["validation"], frames["test"]
    rep = {"threshold": model.threshold, "threshold_source": model.threshold_source, "features": list(model.features)}
    for tag, f in (("validation", val), ("test", test)):
        s = model.score(f)
        rep[tag] = E.evaluate_split(f, s, model.threshold, step_s=step_s)
        inc = (f["status"] == "included").to_numpy()
        if tag == "test":
            rep["ranking_test"] = E.ranking_metrics(f.loc[inc, "label"], s[inc])
            rep["bootstrap_test"] = E.group_bootstrap(f, s, model.threshold, seed=0)
            if with_curves:
                rep["_curves"] = E.curves(f.loc[inc, "label"], s[inc])
    return rep


def _breakdown(test: pd.DataFrame, model: FittedModel, key: str) -> dict:
    out = {}
    s_all = model.score(test)
    for k, g in test.groupby(key):
        idx = test.index.get_indexer(g.index)
        inc = (g["status"] == "included").to_numpy()
        m = E.metrics_from_confusion(E.confusion(g.loc[inc, "label"], s_all[idx][inc] >= model.threshold))
        out[str(k)] = {x: m[x] for x in ("positives", "negatives", "alarms", "tp", "fp", "precision", "recall", "f1", "false_alarm_rate")}
        p = m["prevalence"]
        out[str(k)]["always_alarm_f1"] = None if not p else 2 * p / (1 + p)
    return out


def _importances(models: dict) -> dict:
    out = {}
    rf = models.get("random_forest")
    if rf is not None:
        est = rf.estimator.named_steps["clf"]
        names = list(rf.features) + [f"{rf.features[i]} (missing)" for i in rf.estimator.named_steps["impute"].indicator_.features_.tolist()]
        out["Random forest: impurity importance"] = dict(zip(names, map(float, est.feature_importances_)))
    lr = models.get("logreg")
    if lr is not None:
        est = lr.estimator.named_steps["clf"]
        names = list(lr.features) + [f"{lr.features[i]} (missing)" for i in lr.estimator.named_steps["impute"].indicator_.features_.tolist()]
        out["Logistic regression: standardised coefficient"] = dict(zip(names, map(float, est.coef_[0])))
    return out


def _pending(reasons, ds: DatasetResult | None, cfg, by, seed, out: Path) -> dict:
    res = {"schema_version": SCHEMA_VERSION, "status": "pending", "pipeline_version": PIPELINE_VERSION,
           "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "message": "EVALUATION PENDING: not enough genuine data to train and evaluate. No model was trained or saved and no score is reported.",
           "pending_reasons": list(reasons), "config": cfg.to_dict(), "split_by": by, "seed": seed,
           "data": None if ds is None else {"runs_used": len(ds.runs), "runs_skipped": ds.skipped, "class_balance": ds.meta.get("class_balance"),
                                            "data_origins": ds.meta.get("data_origins")},
           "caveats": CAVEATS}
    (out / "results.json").write_text(json.dumps(res, indent=2, default=_jd))
    (out / "RESULTS.md").write_text(render_markdown(res))
    return res


def run_pipeline(raw_dir=None, out_dir="data/ml/stage6", *, cfg: MLConfig | None = None, by: str = "scenario", seed: int = 0,
                 min_data: MinData | None = None, dataset_dir=None, make_plots: bool = True, save_model: bool = True) -> dict:
    cfg, min_data = cfg or MLConfig(), min_data or MinData()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if dataset_dir is not None:
        ds = load_dataset(dataset_dir)
        cfg = MLConfig.from_dict(ds.meta["config"])
    else:
        ds = build_dataset(raw_dir, cfg, out / "dataset")
    df = ds.df
    if df.empty or (df["status"] == "included").sum() == 0:
        return _pending(["no usable decision points were produced from the raw data"], ds, cfg, by, seed, out)
    try:
        strat = "dwell_model" if (df["dwell_model"].fillna("") != "").any() else None
        split = group_split(df, by=by, seed=seed, min_data=min_data, stratify=strat)
    except InsufficientDataError as e:
        return _pending(e.reasons, ds, cfg, by, seed, out)
    frames = split_frames(df, split)
    assert_no_leakage(frames, by)
    step_s = cfg.sampling.step_s
    balance = {s: class_balance(frames[s]) for s in SPLITS}

    # ---- primary models: all features ----------------------------------------------------------------------------------
    models = fit_all(frames["train"], FEATURE_SETS["all"], seed)
    for n in ("logreg", "random_forest"):
        m = models[n]
        val = frames["validation"]
        inc = (val["status"] == "included").to_numpy()
        m.threshold, m.threshold_source = E.select_threshold(val.loc[inc, "label"], m.score(val)[inc])
    reports = {n: _model_report(n, m, frames, step_s, with_curves=True) for n, m in models.items()}
    curves = {n: r.pop("_curves", {}) for n, r in reports.items()}
    test_inc = frames["test"][frames["test"]["status"] == "included"]
    reference = E.reference_rows(test_inc["label"])
    reference["always_alarm"]["note"] = "reference computed from the labels only (no model): alarm at every decision point"

    # ---- ablation: measurements only (no handoff-history features) -------------------------------------------------------
    abl_models = fit_all(frames["train"], FEATURE_SETS["measurements_only"], seed, names=("logreg", "random_forest"))
    for n, m in abl_models.items():
        val = frames["validation"]
        inc = (val["status"] == "included").to_numpy()
        m.threshold, m.threshold_source = E.select_threshold(val.loc[inc, "label"], m.score(val)[inc])
    ablation = {n: _model_report(n, m, frames, step_s) for n, m in abl_models.items()}
    for r in ablation.values():
        r.pop("_curves", None)

    breakdown = {"by_scenario_test": {n: _breakdown(frames["test"], models[n], "scenario_id") for n in ("logreg", "random_forest")},
                 "by_approach_test": {n: _breakdown(frames["test"], models[n], "approach") for n in ("logreg", "random_forest")}}
    if (frames["test"]["dwell_model"] != "").any():
        breakdown["by_dwell_model_test"] = {n: _breakdown(frames["test"][frames["test"]["dwell_model"] != ""], models[n], "dwell_model")
                                            for n in ("logreg", "random_forest")}
    prevalence = balance["test"]["prevalence"]
    always_f1 = reference["always_alarm"]["f1"]
    findings = []
    for n, r in reports.items():
        f1 = r["test"]["f1"]
        findings.append({"model": n, "test_f1": f1, "always_alarm_f1": always_f1,
                         "beats_always_alarm_f1": (f1 is not None and always_f1 is not None and f1 > always_f1)})
    for n, r in ablation.items():
        findings.append({"model": n + "_measurements_only", "test_f1": r["test"]["f1"], "always_alarm_f1": always_f1,
                         "beats_always_alarm_f1": (r["test"]["f1"] is not None and always_f1 is not None and r["test"]["f1"] > always_f1)})

    for n, groups in breakdown.get("by_dwell_model_test", {}).items():
        for g, m in groups.items():
            if m["f1"] is not None and m["always_alarm_f1"] is not None:
                findings.append({"model": f"{n}[dwell={g}]", "test_f1": m["f1"], "always_alarm_f1": m["always_alarm_f1"],
                                 "beats_always_alarm_f1": m["f1"] > m["always_alarm_f1"]})
    results = {"schema_version": SCHEMA_VERSION, "status": "evaluated", "pipeline_version": PIPELINE_VERSION,
               "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "data": {"origin": ds.meta.get("data_origins"), "raw_dir": ds.meta.get("raw_dir"), "dataset_sha256": ds.meta.get("dataset_sha256"),
                        "runs_used": len(ds.runs), "runs_skipped": ds.skipped, "class_balance_overall": ds.meta.get("class_balance"),
                        "per_split": balance, "step_s": step_s},
               "split": {**split.to_dict(), "stratified_by": strat}, "min_data": vars(min_data), "config": cfg.to_dict(),
               "models": reports, "ablation_measurements_only": ablation, "reference": reference, "breakdown": breakdown,
               "findings": findings, "baseline_rule": models["threshold_baseline"].estimator.to_dict(),
               "caveats": CAVEATS}
    (out / "results.json").write_text(json.dumps(results, indent=2, default=_jd))
    _results_csv(results, out / "results.csv")
    (out / "RESULTS.md").write_text(render_markdown(results))
    if make_plots:
        _plots(results, models, curves, frames, out)
    if save_model:
        save_bundle(out / "model", models=models, cfg=cfg, split=split.to_dict(), dataset_meta=ds.meta, metrics={"models": reports, "reference": reference, "split_balance": balance, "caveats": CAVEATS},
                    extra_metadata={"seed": seed, "split_by": by, "train_rows": int(len(frames["train"])), "feature_set": "all"})
    return results


def _plots(results, models, curves, frames, out: Path):
    from . import plots as P
    pd_ = out / "plots"
    pd_.mkdir(exist_ok=True)
    files = [P.plot_class_balance(results["data"]["per_split"], pd_),
             P.plot_pr(curves, results["data"]["per_split"]["test"]["prevalence"], pd_),
             P.plot_roc({n: c for n, c in curves.items() if c}, pd_),
             P.plot_metric_bars(results["models"], results["reference"], pd_),
             P.plot_warning_times(results["models"], pd_),
             P.plot_importance(_importances(models), pd_)]
    test = frames["test"]
    if len(test):
        exp = test.groupby("experiment_id").size().sort_values().index[len(test.groupby("experiment_id")) // 2]
        g = test[test["experiment_id"] == exp].sort_values("t_ns")
        eps = pd.DataFrame(g).drop_duplicates("episode_event_id")
        onsets = (g.loc[(g["label"] == 1), "t_rel_s"] + g.loc[(g["label"] == 1), "time_to_onset_s"]).round(3).unique().tolist()
        scores = {n: models[n].score(g) for n in ("logreg", "random_forest")}
        files.append(P.plot_timeline(g, scores, {n: models[n].threshold for n in scores}, onsets, pd_, f"held-out run {exp}"))
    results["plots"] = [Path(f).name for f in files]
    (out / "results.json").write_text(json.dumps(results, indent=2, default=_jd))


def _results_csv(results, path: Path):
    rows = []
    def add(group, name, m):
        rows.append({"group": group, "model": name, "threshold": m["threshold"], "precision": m["test"]["precision"], "recall": m["test"]["recall"],
                     "f1": m["test"]["f1"], "false_alarm_rate": m["test"]["false_alarm_rate"],
                     "false_alarm_events_per_hour": m["test"]["false_alarm_events_per_hour"],
                     "tp": m["test"]["tp"], "fp": m["test"]["fp"], "tn": m["test"]["tn"], "fn": m["test"]["fn"],
                     "episodes_assessable": m["test"]["warning"]["episodes_assessable"], "episodes_detected": m["test"]["warning"]["episodes_detected"],
                     "warning_median_s": m["test"]["warning"]["warning_time_s"]["median"],
                     "average_precision": m["ranking_test"]["average_precision"], "roc_auc": m["ranking_test"]["roc_auc"]})
    for n, m in results["models"].items():
        add("all_features", n, m)
    for n, m in results["ablation_measurements_only"].items():
        add("measurements_only", n, m)
    for n, m in (("always_alarm", results["reference"]["always_alarm"]), ("never_alarm", results["reference"]["never_alarm"])):
        rows.append({"group": "reference", "model": n, "threshold": None, "precision": m["precision"], "recall": m["recall"], "f1": m["f1"],
                     "false_alarm_rate": m["false_alarm_rate"], "tp": m["tp"], "fp": m["fp"], "tn": m["tn"], "fn": m["fn"]})
    pd.DataFrame(rows).to_csv(path, index=False, float_format="%.6g")


def _fmt(x, nd=3):
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def render_markdown(r: dict) -> str:
    L = ["# Handoff-degradation prediction: results", "",
         "> Generated by `app.ml` from the files next to it. Simulated network measurements only (no radio signal strength).", ""]
    if r["status"] == "pending":
        L += ["## Status: EVALUATION PENDING", "", r["message"], "", "Reasons:"] + [f"* {x}" for x in r["pending_reasons"]]
        return "\n".join(L) + "\n"
    d = r["data"]
    L += [f"## Status: evaluated (split by **{r['split']['by']}**, seed {r['split']['seed']}, seed attempt {r['split']['seed_attempt_used']})", "",
          f"Data origin: {', '.join(d['origin'] or [])}; runs used: {d['runs_used']}; runs skipped: {len(d['runs_skipped'])}; dataset sha256 `{(d['dataset_sha256'] or '')[:16]}...`", "",
          "### Class imbalance (included decision points)", "", "| split | positives | negatives | prevalence | excluded in-episode | excluded censored | episodes |", "|---|---|---|---|---|---|---|"]
    for s, b in d["per_split"].items():
        L.append(f"| {s} | {b['positives']} | {b['negatives']} | {_fmt(b['prevalence'], 4)} | {b['excluded_in_episode']} | {b['excluded_censored_horizon']} | {b['episodes_with_positive_examples']} |")
    L += ["", "### Held-out test metrics", "", "| model | features | threshold | precision | recall | F1 | false-alarm rate | false alarms/h | episodes detected | median warning (s) | AP |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    def row(n, m, feats):
        t = m["test"]
        w = t["warning"]
        return (f"| {n} | {feats} | {_fmt(m['threshold'], 3)} | {_fmt(t['precision'])} | {_fmt(t['recall'])} | {_fmt(t['f1'])} | {_fmt(t['false_alarm_rate'])} | "
                f"{_fmt(t['false_alarm_events_per_hour'], 1)} | {w['episodes_detected']}/{w['episodes_assessable']} | {_fmt(w['warning_time_s']['median'], 2)} | {_fmt(m['ranking_test']['average_precision'])} |")
    for n, m in r["models"].items():
        L.append(row(MODEL_LABELS[n], m, "all"))
    for n, m in r["ablation_measurements_only"].items():
        L.append(row(MODEL_LABELS[n], m, "measurements only"))
    a, z = r["reference"]["always_alarm"], r["reference"]["never_alarm"]
    L.append(f"| always alarm (reference) | - | - | {_fmt(a['precision'])} | {_fmt(a['recall'])} | {_fmt(a['f1'])} | {_fmt(a['false_alarm_rate'])} | - | - | - | - |")
    L += ["", "Baseline rule: `" + r["baseline_rule"]["rule"] + "`" + (" (degenerate: never alarms)" if r["baseline_rule"]["degenerate_never_alarms"] else ""), "",
          "### Mechanical findings", ""]
    for f in r["findings"]:
        L.append(f"* `{f['model']}`: test F1 {_fmt(f['test_f1'])} vs always-alarm F1 {_fmt(f['always_alarm_f1'])} -> {'above' if f['beats_always_alarm_f1'] else 'NOT above'} the always-alarm reference")
    L += ["", "### Caveats", ""] + [f"* {c}" for c in r["caveats"]]
    return "\n".join(L) + "\n"
