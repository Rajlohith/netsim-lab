"""python -m app.ml <command>   (see docs/HANDOFF_PREDICTION.md)"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import MLConfig, MinData


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.ml", description="Handoff-degradation prediction (Stage 6; simulated data only)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("features", help="print the feature schema, availability rules and the unavailable features")
    g = sub.add_parser("generate", help="run controlled experiments on the emulator (writes raw runs, resumable)")
    g.add_argument("--raw-dir", type=Path, default=Path("data/ml/raw"))
    g.add_argument("--seeds", type=int, default=3, help="runs (movement schedules) per scenario family")
    g.add_argument("--duration", type=float, default=40.0)
    g.add_argument("--base-seed", type=int, default=20260601)
    g.add_argument("--small", action="store_true", help="tiny smoke-test set (one rate/detect delay, uniform dwell)")
    d = sub.add_parser("dataset", help="build dataset.csv from raw runs")
    d.add_argument("--raw-dir", type=Path, required=True)
    d.add_argument("--out-dir", type=Path, default=Path("data/ml/stage6/dataset"))
    d.add_argument("--horizon", type=float, default=None)
    d.add_argument("--step", type=float, default=None)
    t = sub.add_parser("pipeline", help="dataset -> split -> train -> evaluate -> bundle + results + plots (or 'pending')")
    src = t.add_mutually_exclusive_group(required=True)
    src.add_argument("--raw-dir", type=Path)
    src.add_argument("--dataset-dir", type=Path, help="reuse an existing dataset directory (dataset.csv + dataset_meta.json)")
    t.add_argument("--out-dir", type=Path, default=Path("data/ml/stage6"))
    t.add_argument("--split-by", choices=("scenario", "experiment"), default="scenario")
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--horizon", type=float, default=None)
    t.add_argument("--step", type=float, default=None)
    t.add_argument("--no-plots", action="store_true")
    pr = sub.add_parser("predict", help="write handoff_predictions.csv/json next to an existing run directory")
    pr.add_argument("run_dir", type=Path)
    pr.add_argument("--model-dir", type=Path, required=True)
    pr.add_argument("--model", default="random_forest")
    a = p.parse_args(argv)

    if a.cmd == "features":
        from .features import FEATURES, UNAVAILABLE_FEATURES, EVENT_CAPABILITY
        for f in FEATURES:
            print(f"{f.name:34} [{f.group:10}] {f.unit:8} {f.definition}")
        print("\nEvent-based features are available only for these approaches:")
        for k, v in EVENT_CAPABILITY.items():
            print(f"  {k}: {sorted(v)}")
        print("\nNOT AVAILABLE (never fabricated):")
        for n, why in UNAVAILABLE_FEATURES:
            print(f"  {n}: {why}")
        return 0
    if a.cmd == "generate":
        from .datagen import APPROACH_CONFIGS, default_families, generate
        fams = default_families(rates=(100.0,), detects=(20.0,), dwells=("uniform",)) if a.small else default_families()
        total = len(fams) * a.seeds
        print(f"generating {total} runs ({len(fams)} families x {a.seeds}) of {a.duration}s on the emulator -> {a.raw_dir}", flush=True)
        man = generate(a.raw_dir, families=fams, seeds_per_family=a.seeds, duration_s=a.duration, base_seed=a.base_seed,
                       progress=lambda i, n, s, r, st: print(f"  [{i}/{n}] {s}/{r}: {st}", flush=True) if (i % 10 == 0 or st != "ok") else None)
        bad = [e for e in man["runs"] if e["status"] != "ok"]
        print(f"done: {len(man['runs']) - len(bad)} ok, {len(bad)} not ok; manifest {a.raw_dir / 'generation_manifest.json'}")
        for n in man["not_generated"]:
            print(f"  not generated: {n['approach']} ({n['status']}): {n['reason']}")
        return 1 if bad else 0

    def cfg_from(a):
        base = MLConfig()
        label, sampling = base.label, base.sampling
        from dataclasses import replace
        if getattr(a, "horizon", None) is not None:
            label = replace(label, horizon_s=a.horizon)
        if getattr(a, "step", None) is not None:
            sampling = replace(sampling, step_s=a.step)
        return MLConfig(label, base.features, sampling)

    if a.cmd == "dataset":
        from .dataset import build_dataset
        res = build_dataset(a.raw_dir, cfg_from(a), a.out_dir)
        print(json.dumps({"runs_used": len(res.runs), "runs_skipped": len(res.skipped), "class_balance": res.meta["class_balance"]}, indent=2))
        print(f"written to {a.out_dir}")
        return 0
    if a.cmd == "pipeline":
        from .pipeline import run_pipeline
        r = run_pipeline(a.raw_dir, a.out_dir, cfg=cfg_from(a), by=a.split_by, seed=a.seed, dataset_dir=a.dataset_dir, make_plots=not a.no_plots)
        print((a.out_dir / "RESULTS.md").read_text())
        print(f"status: {r['status']}  (files in {a.out_dir})")
        return 0
    if a.cmd == "predict":
        from .integration import annotate_run
        s = annotate_run(a.run_dir, a.model_dir, a.model)
        print(json.dumps({k: s[k] for k in ("experiment_id", "model_name", "model_version", "decision_points", "alarms", "run_in_training_split", "note")}, indent=2))
        print(f"written: {a.run_dir / 'handoff_predictions.csv'}, {a.run_dir / 'handoff_predictions.json'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
