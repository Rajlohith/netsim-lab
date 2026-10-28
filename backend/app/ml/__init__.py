"""Stage 6: handoff-degradation prediction (scikit-learn, pandas, NumPy, Matplotlib).

Pipeline (see docs/HANDOFF_PREDICTION.md):

    raw run directories (Stages 3-5)  ->  runs.RunData
        -> features.extract_features(run, t)        only observations with timestamp <= t
        -> labels.detect_episodes / label_decision_points   (uses the FUTURE, labels only)
        -> dataset.build_dataset                    one row per (experiment, decision time)
        -> splits.group_split                       split by scenario family / experiment, never by row
        -> models (threshold baseline, logistic regression, random forest) -> evaluate -> bundle
        -> predict.HandoffPredictor / integration.annotate_run

Everything here works on SIMULATED network measurements (the Stage 4/5 emulator). Nothing models radio signal strength.
"""
from .config import MLConfig, PIPELINE_VERSION, SCHEMA_VERSION  # noqa: F401
