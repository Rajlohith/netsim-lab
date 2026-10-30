"""Read-only view of the trained Stage 6 model bundles configured by the server (NETSIM_API_MODEL_DIRS).

Clients pick a bundle by ID; they never supply a path. The API process does NOT unpickle anything: it only reads JSON and checks
the MANIFEST sha256 values. Predictions are computed in the worker process (app.ml.integration.annotate_run -> load_bundle, which
verifies the manifest again before unpickling)."""
from __future__ import annotations

import hashlib
import json
from importlib import metadata
from pathlib import Path

from .errors import NotFound
from .settings import ServiceSettings


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _installed_sklearn() -> str | None:
    try:
        return metadata.version("scikit-learn")
    except metadata.PackageNotFoundError:
        return None


class ModelRegistry:
    def __init__(self, settings: ServiceSettings):
        self.settings = settings
        self._cache: dict = {}

    def ids(self) -> list[str]:
        return sorted(self.settings.model_dirs)

    def path(self, model_id: str) -> Path:
        if model_id not in self.settings.model_dirs:
            raise NotFound(f"unknown prediction model {model_id!r}; known: {self.ids()}")
        return Path(self.settings.model_dirs[model_id])

    def _inspect(self, model_id: str) -> dict:
        p = self.path(model_id)
        mf = p / "MANIFEST.json"
        try:
            key = (mf.stat().st_mtime_ns, mf.stat().st_size)
        except OSError:
            return {"id": model_id, "available": False, "reason": f"no model bundle at the configured location (MANIFEST.json missing)"}
        if self._cache.get(model_id, (None,))[0] == key:
            return self._cache[model_id][1]
        info: dict = {"id": model_id, "available": False, "reason": None}
        try:
            manifest = json.loads(mf.read_text())
            for rel, digest in manifest["files"].items():
                f = (p / rel)
                if not f.is_file():
                    raise ValueError(f"bundle file missing: {rel}")
                if _sha(f) != digest:
                    raise ValueError(f"sha256 mismatch for {rel}: bundle was modified or corrupted")
            meta = json.loads((p / "training_metadata.json").read_text())
            saved = str(meta["libraries"]["scikit-learn"]).split(".")[:2]
            now = _installed_sklearn()
            if now is None:
                raise ValueError("scikit-learn is not installed; predictions cannot be computed")
            if saved != now.split(".")[:2]:
                raise ValueError(f"bundle trained with scikit-learn {meta['libraries']['scikit-learn']}, installed {now}: pickles may not load")
            info.update(available=True, reason=None, model_version=meta["model_version"], algorithms=sorted(meta["models"]),
                        created_utc=meta.get("created_utc"), data_origin=meta.get("data_origin"), dataset_sha256=meta.get("dataset_sha256"),
                        class_balance=meta.get("class_balance"), disclaimer=meta.get("disclaimer"),
                        label_horizon_s=(meta.get("config") or {}).get("label", {}).get("horizon_s"),
                        manifest_verified=True)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
            info.update(available=False, reason=f"{type(e).__name__}: {e}", manifest_verified=False)
        self._cache[model_id] = (key, info)
        return info

    def describe(self, model_id: str) -> dict:
        return dict(self._inspect(model_id))

    def list(self) -> list[dict]:
        return [self.describe(i) for i in self.ids()]

    def require_usable(self, model_id: str, algorithm: str | None) -> tuple[bool, str | None]:
        """(usable, reason) for a prediction request."""
        if model_id not in self.settings.model_dirs:
            return False, f"unknown prediction model {model_id!r}; known: {self.ids()}"
        info = self._inspect(model_id)
        if not info["available"]:
            return False, f"model {model_id!r} is not usable: {info['reason']}"
        if algorithm is not None and algorithm not in info["algorithms"]:
            return False, f"algorithm {algorithm!r} is not in model {model_id!r}; available: {info['algorithms']}"
        return True, None

    def evaluation(self, model_id: str) -> dict:
        """Held-out evaluation recorded when the bundle was trained. `results.json` (next to the bundle directory) is only used when its
        dataset hash matches the bundle's; otherwise only the bundle's own metrics.json is reported."""
        info = self._inspect(model_id)
        if not info["available"]:
            raise NotFound(f"model {model_id!r} has no usable bundle: {info['reason']}", code="model_unavailable")
        p = self.path(model_id)
        metrics = json.loads((p / "metrics.json").read_text())
        out = {"model": {k: info[k] for k in ("id", "model_version", "algorithms", "created_utc", "dataset_sha256", "disclaimer")},
               "data_origin": info.get("data_origin"), "is_live": False,
               "held_out_test_metrics": {k: v for k, v in (metrics.get("models") or {}).items()},
               "reference": metrics.get("reference"), "split_balance": metrics.get("split_balance"), "caveats": metrics.get("caveats"),
               "source_files": ["metrics.json", "training_metadata.json"], "full_results": None}
        res = p.parent / "results.json"
        if res.is_file():
            try:
                r = json.loads(res.read_text())
                if (r.get("data") or {}).get("dataset_sha256") == info["dataset_sha256"]:
                    out["full_results"] = {"status": r.get("status"), "generated_utc": r.get("generated_utc"), "split": r.get("split"),
                                           "findings": r.get("findings"), "baseline_rule": r.get("baseline_rule"), "caveats": r.get("caveats"),
                                           "ablation_measurements_only": r.get("ablation_measurements_only"), "breakdown": r.get("breakdown"),
                                           "source_file": "results.json (same dataset sha256 as the bundle)"}
                    out["source_files"].append("../results.json")
            except (OSError, json.JSONDecodeError):
                pass
        out["interpretation_note"] = ("Trained and evaluated on SIMULATED data in which a handoff is an abrupt link change with no gradual pre-handoff "
                                      "degradation. 'findings' compares each model's test F1 with the always-alarm reference; a model that does not beat "
                                      "it has shown no skill on these data. Nothing here says anything about real networks.")
        return out
