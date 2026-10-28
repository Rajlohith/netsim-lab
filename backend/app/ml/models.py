"""The three models: threshold baseline, logistic regression, random forest. All consume the same feature table and return
P(degradation episode starts within the horizon). Hyper-parameters are fixed (no tuning on test data); the alarm threshold of
the two learned models is chosen on the VALIDATION split (evaluate.select_threshold)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# Symptom features a classical reactive monitor would watch ('alarm when it rises above X'). Direction is always '>='.
BASELINE_CANDIDATES = ("rtt_trend_ms", "rtt_slope_ms_per_s", "rtt_std_short_ms", "jitter_ms", "loss_rate_short",
                       "loss_rate_long", "echo_overdue_ratio", "retransmit_count_long", "timeout_failure_count_long")
# (rising RTT = positive trend/slope.) A threshold must mean "a symptom is present": features that are 0 (or below one probe interval) in steady state may not use
# thresholds at or below that steady-state level, otherwise the 'rule' degenerates into always-alarm or into a probe-clock-phase artefact.
POSITIVE_ONLY = {"rtt_trend_ms", "rtt_slope_ms_per_s", "rtt_std_short_ms", "jitter_ms", "loss_rate_short", "loss_rate_long", "retransmit_count_long", "timeout_failure_count_long"}
FLOORS = {"echo_overdue_ratio": 1.0}


class ThresholdBaseline:
    """Single-feature rule: alarm iff feature >= threshold. The (feature, threshold) pair is the one with the best F1 on the
    TRAINING data over a fixed candidate grid. Hard 0/1 output (no scores, so no AUC)."""
    kind = "threshold_baseline"

    def __init__(self, candidates=BASELINE_CANDIDATES):
        self.candidates = tuple(candidates)
        self.feature = None
        self.threshold = None
        self.degenerate = None
        self.fit_summary = None

    def fit(self, X: pd.DataFrame, y):
        y = np.asarray(y).astype(bool)
        best, table = None, []
        for f in self.candidates:
            if f not in X.columns:
                continue
            x = X[f].to_numpy(dtype=float)
            ok = ~np.isnan(x)
            if ok.sum() == 0:
                table.append({"feature": f, "best_f1": None, "threshold": None, "note": "no observations"})
                continue
            grid = np.unique(np.nanquantile(x[ok], np.linspace(0.5, 0.995, 100)))
            if f in POSITIVE_ONLY:
                grid = grid[grid > 1e-6]       # strictly above the steady-state level (0 up to float noise)
            if f in FLOORS:
                grid = grid[grid >= FLOORS[f]]
            if len(grid) == 0:
                table.append({"feature": f, "best_f1": None, "threshold": None, "note": "no admissible threshold (feature never shows a symptom in training data)"})
                continue
            f1b, thb = -1.0, None
            for th in grid:
                pred = ok & (x >= th)
                tp = int((pred & y).sum()); fp = int((pred & ~y).sum()); fn = int((~pred & y).sum())
                f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0
                if f1 > f1b or (f1 == f1b and thb is not None and th > thb):
                    f1b, thb = f1, float(th)
            table.append({"feature": f, "best_f1": round(f1b, 6), "threshold": thb})
            if best is None or f1b > best[0]:
                best = (f1b, f, thb)
        if best is None or best[0] <= 0.0:
            self.feature, self.threshold, self.degenerate = self.candidates[0], float("inf"), True      # never alarms
        else:
            _, self.feature, self.threshold = best
            self.degenerate = False
        self.fit_summary = table
        return self

    def predict_proba(self, X: pd.DataFrame):
        x = X[self.feature].to_numpy(dtype=float)
        fire = (~np.isnan(x)) & (x >= self.threshold)
        p = fire.astype(float)
        return np.column_stack([1 - p, p])

    def to_dict(self):
        return {"kind": self.kind, "candidates": list(self.candidates), "feature": self.feature,
                "threshold": None if self.threshold in (None, float("inf")) else self.threshold,
                "threshold_is_infinite": self.threshold == float("inf"), "degenerate_never_alarms": self.degenerate,
                "rule": f"alarm iff {self.feature} >= {self.threshold}", "fit_on": "training split only (best F1 per feature over a quantile grid)",
                "per_feature_fit": self.fit_summary}

    @classmethod
    def from_dict(cls, d):
        m = cls(d["candidates"])
        m.feature, m.degenerate = d["feature"], d["degenerate_never_alarms"]
        m.threshold = float("inf") if d.get("threshold_is_infinite") else d["threshold"]
        m.fit_summary = d.get("per_feature_fit")
        return m


def _imputer():
    # median of the TRAINING rows; keep_empty_features: a feature that is unavailable in all training rows becomes a constant 0 column
    return SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)


def build_logreg(seed: int = 0) -> Pipeline:
    return Pipeline([("impute", _imputer()), ("scale", StandardScaler()),
                     ("clf", LogisticRegression(class_weight="balanced", C=1.0, max_iter=5000, solver="lbfgs", random_state=seed))])


def build_random_forest(seed: int = 0) -> Pipeline:
    return Pipeline([("impute", _imputer()),
                     ("clf", RandomForestClassifier(n_estimators=200, min_samples_leaf=5, max_features="sqrt",
                                                    class_weight="balanced_subsample", n_jobs=1, random_state=seed))])


MODEL_BUILDERS = {"logreg": build_logreg, "random_forest": build_random_forest}
MODEL_LABELS = {"threshold_baseline": "Threshold baseline", "logreg": "Logistic regression", "random_forest": "Random forest"}


@dataclass
class FittedModel:
    name: str
    features: tuple
    estimator: object            # ThresholdBaseline | sklearn Pipeline
    threshold: float = 0.5       # alarm iff score >= threshold
    threshold_source: str = "default 0.5"

    def score(self, df: pd.DataFrame) -> np.ndarray:
        return self.estimator.predict_proba(df[list(self.features)])[:, 1]

    def alarm(self, df: pd.DataFrame) -> np.ndarray:
        return self.score(df) >= self.threshold


def fit_all(train: pd.DataFrame, features, seed: int = 0, *, names=("threshold_baseline", "logreg", "random_forest")) -> dict:
    inc = train[train["status"] == "included"]
    X, y = inc[list(features)], inc["label"].to_numpy()
    out = {}
    for n in names:
        if n == "threshold_baseline":
            est = ThresholdBaseline().fit(X, y)
            out[n] = FittedModel(n, tuple(features), est, 0.5, "hard rule output (1.0 when the rule fires)")
        else:
            est = MODEL_BUILDERS[n](seed).fit(X, y)
            out[n] = FittedModel(n, tuple(features), est)
    return out


def describe_preprocessing(model: FittedModel) -> dict:
    est = model.estimator
    if isinstance(est, ThresholdBaseline):
        return {"model": model.name, "type": "none (rule on raw feature values; NaN never fires)"}
    imp = est.named_steps["impute"]
    d = {"model": model.name, "feature_order": list(model.features),
         "imputer": {"strategy": "median", "add_indicator": True, "keep_empty_features": True,
                     "fitted_median_per_feature": {f: (None if np.isnan(v) else float(v)) for f, v in zip(model.features, imp.statistics_)},
                     "indicator_for_features": [model.features[i] for i in imp.indicator_.features_.tolist()]}}
    if "scale" in est.named_steps:
        sc = est.named_steps["scale"]
        d["scaler"] = {"type": "StandardScaler", "mean": [float(x) for x in sc.mean_], "scale": [float(x) for x in sc.scale_],
                       "note": "columns = imputed features followed by missing-value indicators"}
    else:
        d["scaler"] = None
    return d
