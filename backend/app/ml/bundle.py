"""Model bundle = a directory with everything needed to reuse (and audit) a trained model:

  feature_schema.json     feature names, units, windows, availability rules, unavailable features
  preprocessing.json      per model: feature order, imputer medians, scaler statistics
  label_definition.json   target, horizon, exact labelling rules + parameters
  thresholds.json         alarm threshold per model and how it was chosen
  models/*.joblib|json    fitted estimators (the threshold baseline is plain JSON)
  metrics.json            held-out test metrics at the time of training (copy of results.json 'models')
  training_metadata.json  model_version, library versions, seed, dataset hash, split membership, class balance, timestamps
  MANIFEST.json           sha256 of every file above

SECURITY: .joblib files are pickles. Only load bundles you created or trust; load_bundle verifies the sha256 in MANIFEST.json
before unpickling, which detects accidental corruption and casual tampering, NOT a malicious bundle whose manifest was rewritten.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn

from .config import MLConfig, PIPELINE_VERSION, SCHEMA_VERSION
from .features import FEATURE_NAMES, schema_dict
from .models import FittedModel, ThresholdBaseline, describe_preprocessing


class BundleError(Exception):
    pass


LABEL_DEFINITION_TEXT = {
    "target": "a handoff-related connectivity degradation episode STARTS within the next horizon_s seconds",
    "episode": "a handoff (events.csv handoff_start) accompanied by an echo-arrival gap >= max(min_gap_ms, gap_interval_factor * probe interval) "
               "that overlaps [handoff_start, handoff_end + handoff_margin_ms]",
    "onset": "the handoff_start instant (link change), not the first symptom seen by the sender",
    "positive": "label = 1 iff an episode onset lies in (t, t + horizon_s]",
    "excluded": ["excluded_in_episode: onset <= t <= episode end (degradation already under way)",
                 "excluded_censored_horizon: t + horizon_s is after the end of the observed run (label unknowable)"],
    "decision_times": "first probe + min_history_s, then every step_s",
    "features": "only observations with timestamp <= t (enforced by extract_features, tested by truncating the run at t)",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _versions() -> dict:
    return {"python": platform.python_version(), "platform": platform.platform(), "numpy": np.__version__, "pandas": pd.__version__,
            "scikit-learn": sklearn.__version__, "joblib": joblib.__version__}


@dataclass
class Bundle:
    path: Path
    models: dict
    cfg: MLConfig
    metadata: dict
    label_definition: dict
    feature_schema: dict
    preprocessing: dict
    thresholds: dict
    metrics: dict
    manifest: dict = field(default_factory=dict)

    @property
    def model_version(self) -> str:
        return self.metadata["model_version"]


def save_bundle(out_dir, *, models: dict, cfg: MLConfig, split: dict, dataset_meta: dict, metrics: dict, extra_metadata: dict | None = None) -> dict:
    out = Path(out_dir)
    (out / "models").mkdir(parents=True, exist_ok=True)
    ds_sha = dataset_meta.get("dataset_sha256") or "unknown"
    version = f"{PIPELINE_VERSION}+d{ds_sha[:8]}.c{cfg.fingerprint()[:8]}"
    for n, m in models.items():
        if isinstance(m.estimator, ThresholdBaseline):
            (out / "models" / f"{n}.json").write_text(json.dumps(m.estimator.to_dict(), indent=2))
        else:
            joblib.dump(m.estimator, out / "models" / f"{n}.joblib", compress=3)
    (out / "feature_schema.json").write_text(json.dumps(schema_dict(), indent=2))
    (out / "preprocessing.json").write_text(json.dumps({n: describe_preprocessing(m) for n, m in models.items()}, indent=2))
    (out / "label_definition.json").write_text(json.dumps({**LABEL_DEFINITION_TEXT, "parameters": cfg.to_dict()["label"], "sampling": cfg.to_dict()["sampling"],
                                                          "feature_windows": cfg.to_dict()["features"]}, indent=2))
    (out / "thresholds.json").write_text(json.dumps({n: {"threshold": m.threshold, "source": m.threshold_source, "features": list(m.features)}
                                                     for n, m in models.items()}, indent=2))
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, default=_jd))
    meta = {"schema_version": SCHEMA_VERSION, "model_version": version, "pipeline_version": PIPELINE_VERSION,
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "libraries": _versions(),
            "config": cfg.to_dict(), "config_fingerprint": cfg.fingerprint(), "dataset_sha256": ds_sha,
            "data_origin": dataset_meta.get("data_origins"), "class_balance": dataset_meta.get("class_balance"),
            "split": split, "models": sorted(models), "argv": sys.argv[1:] if sys.argv else [],
            "disclaimer": "Trained on SIMULATED network measurements from the Stage 4/5 emulator. Not valid for real networks; "
                          "no feature is physical radio signal strength.", **(extra_metadata or {})}
    (out / "training_metadata.json").write_text(json.dumps(meta, indent=2, default=_jd))
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "MANIFEST.json")
    manifest = {"schema_version": SCHEMA_VERSION, "model_version": version,
                "files": {str(p.relative_to(out)): _sha(p) for p in files}}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest


def load_bundle(path, *, verify: bool = True, allow_version_mismatch: bool = False) -> Bundle:
    p = Path(path)
    mf = p / "MANIFEST.json"
    if not mf.exists():
        raise BundleError(f"{p}: not a model bundle (MANIFEST.json missing)")
    manifest = json.loads(mf.read_text())
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleError(f"unsupported bundle schema_version {manifest.get('schema_version')!r} (this code reads {SCHEMA_VERSION})")
    if verify:
        for rel, digest in manifest["files"].items():
            f = p / rel
            if not f.exists():
                raise BundleError(f"bundle file missing: {rel}")
            if _sha(f) != digest:
                raise BundleError(f"sha256 mismatch for {rel}: bundle was modified or corrupted")
    meta = json.loads((p / "training_metadata.json").read_text())
    schema = json.loads((p / "feature_schema.json").read_text())
    if [f["name"] for f in schema["features"]] != list(FEATURE_NAMES):
        raise BundleError("feature schema in the bundle differs from this code version (feature names/order changed): retrain")
    saved, now = meta["libraries"]["scikit-learn"].split(".")[:2], sklearn.__version__.split(".")[:2]
    if saved != now and not allow_version_mismatch:
        raise BundleError(f"bundle trained with scikit-learn {meta['libraries']['scikit-learn']}, running {sklearn.__version__}: "
                          "pickles may not load correctly (pass allow_version_mismatch=True to try anyway)")
    cfg = MLConfig.from_dict(meta["config"])
    thr = json.loads((p / "thresholds.json").read_text())
    pre = json.loads((p / "preprocessing.json").read_text())
    models = {}
    for name, info in thr.items():
        feats = tuple(info["features"])
        unknown = set(feats) - set(FEATURE_NAMES)
        if unknown:
            raise BundleError(f"model {name} uses unknown features {sorted(unknown)}")
        jf, pf = p / "models" / f"{name}.json", p / "models" / f"{name}.joblib"
        est = ThresholdBaseline.from_dict(json.loads(jf.read_text())) if jf.exists() else joblib.load(pf)
        models[name] = FittedModel(name, feats, est, float(info["threshold"]), info["source"])
    return Bundle(p, models, cfg, meta, json.loads((p / "label_definition.json").read_text()), schema, pre, thr,
                  json.loads((p / "metrics.json").read_text()), manifest)


def _jd(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return None if np.isnan(o) else float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))
