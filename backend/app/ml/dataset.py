"""Turn run directories into a table of training examples: one row per (experiment, decision time).

Row = features at t (observations <= t) + label (future) + bookkeeping. Bookkeeping columns (experiment_id, scenario_id,
t_ns, t_rel_s, status, episode_event_id, time_to_onset_s) are NEVER model inputs; models only see FEATURE_NAMES.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import MLConfig, SCHEMA_VERSION
from .features import FEATURE_NAMES, extract_features, feature_availability, schema_dict
from .labels import detect_episodes, label_decision_points
from .runs import RunData, RunLoadError, discover_runs, load_run

META_COLUMNS = ["experiment_id", "scenario_id", "run_id", "approach", "dwell_model", "data_origin", "t_ns", "t_rel_s"]
LABEL_COLUMNS = ["label", "status", "episode_event_id", "time_to_onset_s"]
COLUMNS = META_COLUMNS + list(FEATURE_NAMES) + LABEL_COLUMNS


def decision_times(run: RunData, cfg: MLConfig) -> np.ndarray:
    first, end = run.first_probe_ns, run.observed_end_ns
    if first is None or end is None:
        return np.zeros(0, dtype=np.int64)
    step = int(round(cfg.sampling.step_s * 1e9))
    start = first + int(round(cfg.sampling.min_history_s * 1e9))
    return np.arange(start, end + 1, step, dtype=np.int64)


def build_run_examples(run: RunData, cfg: MLConfig):
    """Returns (DataFrame with COLUMNS, info dict describing the run's labelling outcome)."""
    episodes, outcomes, diag = detect_episodes(run, cfg.label)
    times = decision_times(run, cfg)
    labels = label_decision_points(times, episodes, run.observed_end_ns, cfg.label)
    rows = []
    first = run.first_probe_ns
    for t, lab in zip(times, labels):
        fr = extract_features(run, int(t), cfg.features)
        rows.append({"experiment_id": run.experiment_id, "scenario_id": run.scenario_id, "run_id": run.run_id,
                     "approach": run.approach, "dwell_model": run.tags.get("dwell_model", ""), "data_origin": run.data_origin, "t_ns": int(t),
                     "t_rel_s": (int(t) - first) / 1e9, **fr.values, **lab})
    df = pd.DataFrame(rows, columns=COLUMNS)
    av = feature_availability(run.approach, run.approach_events_present)
    info = {"experiment_id": run.experiment_id, "scenario_id": run.scenario_id, "approach": run.approach, "data_origin": run.data_origin,
            "decision_points": int(len(df)), "status_counts": df["status"].value_counts().to_dict() if len(df) else {},
            "handoffs": len(run.handoffs), "handoff_outcomes": {k: sum(1 for o in outcomes if o["outcome"] == k) for k in ("degraded", "benign", "merged")},
            "episodes": [e.to_dict() for e in episodes], "gap_threshold_ms": diag["gap_threshold_ms"], "unrelated_gaps": diag["unrelated_gaps"],
            "unavailable_features": {k: why for k, (ok, why) in av.items() if not ok}, "warnings": list(run.warnings)}
    return df, info


def class_balance(df: pd.DataFrame) -> dict:
    inc = df[df["status"] == "included"]
    pos, neg = int((inc["label"] == 1).sum()), int((inc["label"] == 0).sum())
    return {"included": int(len(inc)), "positives": pos, "negatives": neg,
            "prevalence": (pos / len(inc)) if len(inc) else None,
            "imbalance_ratio_neg_per_pos": (neg / pos) if pos else None,
            "excluded_in_episode": int((df["status"] == "excluded_in_episode").sum()),
            "excluded_censored_horizon": int((df["status"] == "excluded_censored_horizon").sum()),
            "episodes_with_positive_examples": int(inc.loc[inc["label"] == 1, ["experiment_id", "episode_event_id"]].drop_duplicates().shape[0])}


@dataclass
class DatasetResult:
    df: pd.DataFrame
    runs: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def build_dataset(raw_dir, cfg: MLConfig | None = None, out_dir=None, *, strict_origin: str | None = None) -> DatasetResult:
    """Scan raw_dir for run directories and build the dataset. Runs that cannot be used are LISTED in `skipped` with the reason,
    never dropped silently. If out_dir is given, writes dataset.csv, dataset_meta.json, feature_schema.json."""
    cfg = cfg or MLConfig()
    frames, infos, skipped = [], [], []
    for d in discover_runs(raw_dir):
        try:
            run = load_run(d)
        except RunLoadError as e:
            skipped.append({"run_dir": str(d), "reason": str(e)})
            continue
        if strict_origin and run.data_origin != strict_origin:
            skipped.append({"run_dir": str(d), "reason": f"data_origin {run.data_origin!r} != required {strict_origin!r}"})
            continue
        if not run.handoffs:
            skipped.append({"run_dir": str(d), "reason": "no handoff events in events.csv: nothing to label"})
            continue
        df, info = build_run_examples(run, cfg)
        if df.empty:
            skipped.append({"run_dir": str(d), "reason": "run too short for any decision point"})
            continue
        frames.append(df)
        infos.append(info)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    meta = {"schema_version": SCHEMA_VERSION, "config": cfg.to_dict(), "config_fingerprint": cfg.fingerprint(),
            "raw_dir": str(raw_dir), "runs_used": len(infos), "runs_skipped": skipped,
            "class_balance": class_balance(df) if len(df) else None,
            "by_scenario": {k: class_balance(g) for k, g in df.groupby("scenario_id")} if len(df) else {},
            "by_approach": {str(k): class_balance(g) for k, g in df.groupby("approach")} if len(df) else {},
            "runs": infos,
            "data_origins": sorted(df["data_origin"].unique().tolist()) if len(df) else []}
    res = DatasetResult(df, infos, skipped, meta)
    if out_dir is not None:
        write_dataset(res, out_dir)
    return res


def write_dataset(res: DatasetResult, out_dir) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv = out / "dataset.csv"
    res.df.to_csv(csv, index=False, float_format="%.10g")
    res.meta["dataset_sha256"] = hashlib.sha256(csv.read_bytes()).hexdigest()
    (out / "dataset_meta.json").write_text(json.dumps(res.meta, indent=2, sort_keys=True, default=_json_default))
    (out / "feature_schema.json").write_text(json.dumps(schema_dict(), indent=2))
    return csv


def load_dataset(out_dir) -> DatasetResult:
    out = Path(out_dir)
    df = pd.read_csv(out / "dataset.csv", dtype={"episode_event_id": str, "dwell_model": str})
    df["episode_event_id"] = df["episode_event_id"].fillna("")
    df["dwell_model"] = df["dwell_model"].fillna("")
    meta = json.loads((out / "dataset_meta.json").read_text())
    got = hashlib.sha256((out / "dataset.csv").read_bytes()).hexdigest()
    if meta.get("dataset_sha256") and meta["dataset_sha256"] != got:
        raise ValueError("dataset.csv does not match the hash recorded in dataset_meta.json (file modified?)")
    return DatasetResult(df, meta.get("runs", []), meta.get("runs_skipped", []), meta)


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, float) and o != o:
        return None
    raise TypeError(type(o))
