"""All tunable parameters of the pipeline in one place. They are serialised into every dataset / model bundle, so a
result can always be traced back to the exact labelling rule and feature windows that produced it."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields

SCHEMA_VERSION = 1                 # version of the files this package writes (dataset, bundle, results)
PIPELINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class LabelConfig:
    """Target: 'a handoff-related connectivity degradation episode STARTS within the next `horizon_s` seconds'.

    A *degradation episode* is a handoff (events.csv handoff_start) that is accompanied by a service gap: an interval
    between two consecutive echo arrivals at the probe sender that is at least
        gap_threshold = max(min_gap_ms, gap_interval_factor * nominal probe interval).
    Its *onset* is the handoff_start instant (the moment the link changes), NOT the first symptom seen by the sender.
    """
    horizon_s: float = 1.0
    min_gap_ms: float = 60.0
    gap_interval_factor: float = 3.0
    handoff_margin_ms: float = 250.0       # a gap belongs to a handoff if it overlaps [start, end + margin]
    open_handoff_window_s: float = 5.0     # window used when a handoff has no handoff_end event


@dataclass(frozen=True)
class FeatureConfig:
    short_window_s: float = 1.0
    long_window_s: float = 5.0
    loss_grace_ms: float = 250.0           # a probe younger than this is 'pending', not 'lost' (its echo may still be on the way)
    history_window_s: float = 30.0         # window for 'handoffs started recently'


@dataclass(frozen=True)
class SamplingConfig:
    step_s: float = 0.25                   # spacing of decision times
    min_history_s: float = 2.0             # first decision time = first probe + min_history_s


@dataclass(frozen=True)
class MLConfig:
    label: LabelConfig = field(default_factory=LabelConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MLConfig":
        def mk(klass, sub):
            known = {f.name for f in fields(klass)}
            extra = set(sub) - known
            if extra:
                raise ValueError(f"unknown {klass.__name__} keys: {sorted(extra)}")
            return klass(**sub)
        return cls(label=mk(LabelConfig, d.get("label", {})), features=mk(FeatureConfig, d.get("features", {})),
                   sampling=mk(SamplingConfig, d.get("sampling", {})))

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class MinData:
    """When is there 'sufficient' data to train and evaluate? Below this the pipeline reports evaluation PENDING."""
    min_groups_per_split: int = 2           # scenario families (or experiments) in each of train / validation / test
    min_episodes_per_split: int = 5         # degradation episodes with at least one assessable positive example
    min_positive_examples_per_split: int = 20
    min_train_negatives: int = 20
    min_positive_examples_per_stratum: int = 10   # only when the split is stratified (every split must contain positives from every stratum)
