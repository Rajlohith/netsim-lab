"""Attach predictions to an EXISTING run directory (Stage 3-5 output) without touching any of its files.

Writes two new files next to the raw data:
  handoff_predictions.csv   one row per decision time (prediction timestamps, score, alarm)
  handoff_predictions.json  model identity, provenance, and - computed from the run's own events - which degradation episodes
                            happened and whether/when the model warned. If the run belongs to the model's training split the
                            summary says so: that is NOT a held-out evaluation.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

from .bundle import load_bundle
from .labels import detect_episodes
from .predict import HandoffPredictor
from .runs import load_run

CSV_COLUMNS = ("experiment_id", "model_name", "model_version", "decision_time_ns", "horizon_end_ns", "horizon_s", "score", "alarm",
               "threshold", "last_observation_ns")
PROVENANCE = ("Predictions of SIMULATED handoff degradation from simulated network measurements (RTT, probe loss, jitter, delivered "
              "throughput, handoff history). Not radio signal strength; not valid for real networks.")


def annotate_run(run_dir, model_dir, model_name: str = "random_forest", *, allow_version_mismatch: bool = False) -> dict:
    run_dir = Path(run_dir)
    bundle = load_bundle(model_dir, allow_version_mismatch=allow_version_mismatch)
    run = load_run(run_dir)
    pred = HandoffPredictor(bundle, model_name)
    preds = pred.predict_run(run)
    with open(run_dir / "handoff_predictions.csv", "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CSV_COLUMNS)
        for p in preds:
            d = p.to_dict()
            w.writerow([d[c] if c != "alarm" else int(d[c]) for c in CSV_COLUMNS])
    episodes, outcomes, _ = detect_episodes(run, bundle.cfg.label)
    H = int(round(bundle.cfg.label.horizon_s * 1e9))
    ep_rows = []
    for e in episodes:
        window = [p for p in preds if e.onset_ns - H <= p.decision_time_ns < e.onset_ns]
        first = next((p for p in window if p.alarm), None)
        ep_rows.append({"event_id": e.event_id, "onset_ns": e.onset_ns, "gap_ms": e.gap_ms,
                        "decision_points_in_horizon": len(window), "warned": first is not None,
                        "warning_s": None if first is None else (e.onset_ns - first.decision_time_ns) / 1e9})
    in_train = (run.scenario_id in set(bundle.metadata["split"]["groups"]["train"]) or run.experiment_id in set(bundle.metadata["split"]["groups"]["train"]))
    summary = {"schema_version": 1, "provenance": PROVENANCE, "run_dir": run_dir.name, "experiment_id": run.experiment_id,
               "model_name": model_name, "model_version": bundle.model_version, "threshold": pred.model.threshold,
               "decision_points": len(preds), "alarms": int(sum(p.alarm for p in preds)),
               "ground_truth_episodes": ep_rows, "handoff_outcomes": outcomes,
               "run_in_training_split": bool(in_train),
               "note": ("This run belongs to the model's TRAINING split: the comparison below is not a held-out evaluation." if in_train else
                        "This run is not in the training split of this model (it may be in validation/test, or unseen)."),
               "files": {"csv": "handoff_predictions.csv"}}
    (run_dir / "handoff_predictions.json").write_text(json.dumps(summary, indent=2))
    return summary
