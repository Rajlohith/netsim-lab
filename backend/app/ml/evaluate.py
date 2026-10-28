"""Evaluation on HELD-OUT groups: precision, recall, F1, false-alarm rate, warning time. Nothing here is computed from
training data, and nothing is reported unless the models were actually fitted and scored on a test split."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score, roc_curve


def _ratio(a, b):
    return None if b == 0 else a / b


def confusion(y, alarm) -> dict:
    y = np.asarray(y).astype(bool); a = np.asarray(alarm).astype(bool)
    tp, fp, tn, fn = int((a & y).sum()), int((a & ~y).sum()), int((~a & ~y).sum()), int((~a & y).sum())
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn}


def metrics_from_confusion(c: dict) -> dict:
    tp, fp, tn, fn = c["tp"], c["fp"], c["tn"], c["fn"]
    prec, rec = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    return {**c, "precision": prec, "recall": rec, "f1": _ratio(2 * tp, 2 * tp + fp + fn),
            "false_alarm_rate": _ratio(fp, fp + tn),       # FPR = FP / (FP + TN): share of negative decision points that raised an alarm
            "positives": tp + fn, "negatives": fp + tn, "alarms": tp + fp,
            "prevalence": _ratio(tp + fn, tp + fn + fp + tn),
            "precision_note": "undefined (no alarms raised)" if tp + fp == 0 else None}


def select_threshold(y, score, grid: int = 200):
    """Alarm threshold maximising F1 on the VALIDATION split; ties -> the higher (more conservative) threshold.
    Returns (threshold, source description)."""
    y = np.asarray(y).astype(bool); s = np.asarray(score, dtype=float)
    if y.sum() == 0:
        return 0.5, "default 0.5 (validation split has no positives)"
    cands = np.unique(np.quantile(s, np.linspace(0, 1, grid)))
    best, best_t = -1.0, 0.5
    for t in cands:
        a = s >= t
        tp = int((a & y).sum()); fp = int((a & ~y).sum()); fn = int((~a & y).sum())
        f1 = 2 * tp / (2 * tp + fp + fn) if (2 * tp + fp + fn) else 0.0
        if f1 > best or (f1 == best and t > best_t):
            best, best_t = f1, float(t)
    return best_t, f"max F1 on validation split ({best:.4f})"


def alarm_bursts(df: pd.DataFrame, alarm) -> int:
    """False-alarm EVENTS: maximal runs of consecutive decision points (same experiment, adjacent grid steps) that raise an
    alarm while label == 0. One burst of 5 alarmed points is one false alarm."""
    d = df.assign(_alarm=np.asarray(alarm).astype(bool)).sort_values(["experiment_id", "t_ns"])
    n = 0
    for _, g in d.groupby("experiment_id", sort=False):
        fa = ((g["_alarm"]) & (g["label"] == 0)).to_numpy()
        prev = False
        for x in fa:
            if x and not prev:
                n += 1
            prev = x
    return n


def warning_times(df: pd.DataFrame, alarm) -> dict:
    """Per degradation episode: lead time = onset - time of the FIRST alarm among the episode's positive decision points
    (those within `horizon` before the onset). Episodes with no alarm in that window are missed."""
    d = df.assign(_alarm=np.asarray(alarm).astype(bool))
    pos = d[(d["label"] == 1) & (d["status"] == "included")]
    lead, missed, per = [], 0, []
    for (exp, ep), g in pos.groupby(["experiment_id", "episode_event_id"]):
        hit = g[g["_alarm"]]
        if len(hit):
            w = float(hit["time_to_onset_s"].max())          # earliest alarm = largest remaining time to onset
            lead.append(w)
            per.append({"experiment_id": exp, "episode": ep, "warning_s": w})
        else:
            missed += 1
            per.append({"experiment_id": exp, "episode": ep, "warning_s": None})
    n = len(lead) + missed
    q = (lambda p: float(np.percentile(lead, p))) if lead else (lambda p: None)
    return {"episodes_assessable": n, "episodes_detected": len(lead), "episodes_missed": missed,
            "episode_recall": _ratio(len(lead), n), "warning_time_s": {"median": q(50), "mean": float(np.mean(lead)) if lead else None,
                                                                         "min": float(min(lead)) if lead else None, "max": float(max(lead)) if lead else None},
            "per_episode": per,
            "note": "warning time is quantised by the decision step; it is the lead of the first alarm inside the labelled horizon only"}


def evaluate_split(df: pd.DataFrame, score, threshold: float, *, step_s: float) -> dict:
    inc_mask = (df["status"] == "included").to_numpy()
    d = df[inc_mask]
    s = np.asarray(score, dtype=float)[inc_mask]
    alarm = s >= threshold
    m = metrics_from_confusion(confusion(d["label"].to_numpy(), alarm))
    hours = len(d) * step_s / 3600.0
    m["false_alarm_events"] = alarm_bursts(d, alarm)
    m["false_alarm_events_per_hour"] = (m["false_alarm_events"] / hours) if hours > 0 else None
    m["evaluated_simulated_hours"] = hours
    m["threshold"] = float(threshold)
    m["warning"] = warning_times(d, alarm)
    return m


def ranking_metrics(y, score) -> dict:
    y = np.asarray(y).astype(bool); s = np.asarray(score, dtype=float)
    if y.sum() == 0 or (~y).sum() == 0 or np.unique(s).size < 2:
        return {"average_precision": None, "roc_auc": None, "note": "undefined (single class or constant score)"}
    return {"average_precision": float(average_precision_score(y, s)), "roc_auc": float(roc_auc_score(y, s)),
            "note": "average_precision of a random scorer equals the prevalence"}


def reference_rows(y) -> dict:
    """Trivial reference points computed from labels only (no model): 'never alarm' and 'always alarm'."""
    y = np.asarray(y).astype(bool)
    return {"always_alarm": metrics_from_confusion(confusion(y, np.ones_like(y))),
            "never_alarm": metrics_from_confusion(confusion(y, np.zeros_like(y)))}


def group_bootstrap(df: pd.DataFrame, score, threshold: float, *, n_boot: int = 300, seed: int = 0, key: str = "experiment_id") -> dict:
    """95% percentile intervals from resampling whole experiments (never rows) with replacement. Indicates how much the
    numbers depend on which held-out experiments happened to be drawn; with few groups the intervals are wide."""
    d = df[df["status"] == "included"]
    s = np.asarray(score, dtype=float)[(df["status"] == "included").to_numpy()]
    y = d["label"].to_numpy().astype(bool); a = s >= threshold
    groups = d[key].to_numpy()
    uniq = np.unique(groups)
    if len(uniq) < 3:
        return {"n_groups": int(len(uniq)), "note": "too few groups for a bootstrap"}
    idx = {g: np.nonzero(groups == g)[0] for g in uniq}
    rng = np.random.default_rng(seed)
    out = {k: [] for k in ("precision", "recall", "f1", "false_alarm_rate")}
    for _ in range(n_boot):
        pick = np.concatenate([idx[g] for g in rng.choice(uniq, size=len(uniq), replace=True)])
        m = metrics_from_confusion(confusion(y[pick], a[pick]))
        for k in out:
            if m[k] is not None:
                out[k].append(m[k])
    ci = {k: ([float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] if len(v) >= 20 else None) for k, v in out.items()}
    return {"n_groups": int(len(uniq)), "n_boot": n_boot, "resampled": key, "ci95": ci}


def curves(y, score) -> dict:
    y = np.asarray(y).astype(bool); s = np.asarray(score, dtype=float)
    if y.sum() == 0 or (~y).sum() == 0 or np.unique(s).size < 2:
        return {}
    p, r, _ = precision_recall_curve(y, s)
    fpr, tpr, _ = roc_curve(y, s)
    return {"pr": {"precision": p.tolist(), "recall": r.tolist()}, "roc": {"fpr": fpr.tolist(), "tpr": tpr.tolist()}}
