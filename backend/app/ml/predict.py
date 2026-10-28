"""Online-style prediction: given a run and a decision time t, use observations <= t and answer
'will a handoff degradation episode start within (t, t + horizon]?'."""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np
import pandas as pd

from .bundle import Bundle
from .dataset import decision_times
from .features import extract_features, feature_availability, LeakageError
from .runs import RunData


@dataclass
class Prediction:
    experiment_id: str
    model_name: str
    model_version: str
    decision_time_ns: int             # the instant the prediction is made; the prediction covers (decision_time_ns, horizon_end_ns]
    horizon_s: float
    horizon_end_ns: int
    score: float
    alarm: bool
    threshold: float
    last_observation_ns: int | None   # newest observation used; always <= decision_time_ns
    unavailable_features: list

    def to_dict(self):
        return asdict(self)


class HandoffPredictor:
    def __init__(self, bundle: Bundle, model_name: str = "random_forest"):
        if model_name not in bundle.models:
            raise KeyError(f"model {model_name!r} not in bundle; have {sorted(bundle.models)}")
        self.bundle, self.model_name = bundle, model_name
        self.model = bundle.models[model_name]
        self.cfg = bundle.cfg

    def predict_at(self, run: RunData, t_ns: int) -> Prediction:
        fr = extract_features(run, int(t_ns), self.cfg.features)
        if fr.last_observation_ns is not None and fr.last_observation_ns > t_ns:
            raise LeakageError("observation newer than the decision time")
        row = pd.DataFrame([fr.values])
        score = float(self.model.score(row)[0])
        H = int(round(self.cfg.label.horizon_s * 1e9))
        av = feature_availability(run.approach, run.approach_events_present)
        return Prediction(run.experiment_id, self.model_name, self.bundle.model_version, int(t_ns), self.cfg.label.horizon_s,
                          int(t_ns) + H, score, bool(score >= self.model.threshold), self.model.threshold, fr.last_observation_ns,
                          sorted(k for k, (ok, _) in av.items() if not ok))

    def predict_run(self, run: RunData, times_ns=None) -> list:
        times = decision_times(run, self.cfg) if times_ns is None else np.asarray(times_ns, dtype=np.int64)
        return [self.predict_at(run, int(t)) for t in times]
