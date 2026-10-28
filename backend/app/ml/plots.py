"""Evaluation plots (Matplotlib, headless). Every plot is drawn from the results dict / dataset of an actual run of the pipeline."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from .models import MODEL_LABELS  # noqa: E402

COLORS = {"threshold_baseline": "#888888", "logreg": "#1f77b4", "random_forest": "#d62728"}


def _save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return str(path)


def plot_class_balance(split_balance: dict, out: Path):
    fig, ax = plt.subplots(figsize=(6, 3.6))
    names = list(split_balance)
    pos = [split_balance[s]["positives"] for s in names]
    neg = [split_balance[s]["negatives"] for s in names]
    ax.bar(names, neg, label="negative (no episode starts within horizon)", color="#bbbbbb")
    ax.bar(names, pos, bottom=neg, label="positive", color="#d62728")
    for i, s in enumerate(names):
        prev = split_balance[s]["prevalence"]
        ax.text(i, neg[i] + pos[i], f"{100 * prev:.1f}% positive" if prev is not None else "", ha="center", va="bottom", fontsize=8)
    ax.set_ylabel("decision points (included)")
    ax.set_title("Class imbalance per split")
    ax.legend(fontsize=7)
    return _save(fig, out / "class_balance.png")


def plot_pr(curves: dict, prevalence: float, out: Path):
    fig, ax = plt.subplots(figsize=(5.2, 4.2))
    for n, c in curves.items():
        if c:
            ax.plot(c["pr"]["recall"], c["pr"]["precision"], label=MODEL_LABELS[n], color=COLORS[n])
    if prevalence is not None:
        ax.axhline(prevalence, ls="--", color="k", lw=0.8, label=f"prevalence = {prevalence:.3f} (random / always-alarm precision)")
    ax.set_xlabel("recall"); ax.set_ylabel("precision"); ax.set_title("Precision-recall, held-out test experiments")
    ax.set_xlim(0, 1); ax.set_ylim(0, 1.02); ax.legend(fontsize=7)
    return _save(fig, out / "pr_curves.png")


def plot_roc(curves: dict, out: Path):
    fig, ax = plt.subplots(figsize=(4.6, 4.2))
    for n, c in curves.items():
        if c:
            ax.plot(c["roc"]["fpr"], c["roc"]["tpr"], label=MODEL_LABELS[n], color=COLORS[n])
    ax.plot([0, 1], [0, 1], "k--", lw=0.8, label="chance")
    ax.set_xlabel("false-alarm rate"); ax.set_ylabel("recall"); ax.set_title("ROC, held-out test experiments"); ax.legend(fontsize=7)
    return _save(fig, out / "roc_curves.png")


def plot_metric_bars(models: dict, reference: dict, out: Path):
    rows = {MODEL_LABELS.get(n, n): m["test"] for n, m in models.items()}
    rows["Always alarm (reference)"] = reference["always_alarm"]
    keys = ("precision", "recall", "f1", "false_alarm_rate")
    fig, ax = plt.subplots(figsize=(8, 3.8))
    w = 0.8 / len(rows)
    for i, (n, m) in enumerate(rows.items()):
        vals = [(m[k] if m[k] is not None else 0.0) for k in keys]
        ax.bar(np.arange(len(keys)) + i * w, vals, w, label=n)
    ax.set_xticks(np.arange(len(keys)) + 0.4 - w / 2); ax.set_xticklabels(("precision", "recall", "F1", "false-alarm rate"))
    ax.set_ylim(0, 1.05); ax.set_title("Held-out test metrics (undefined precision shown as 0)"); ax.legend(fontsize=7)
    return _save(fig, out / "test_metrics.png")


def plot_warning_times(models: dict, out: Path):
    data, labels = [], []
    for n, m in models.items():
        per = [e["warning_s"] for e in m["test"]["warning"]["per_episode"] if e["warning_s"] is not None]
        if per:
            data.append(per); labels.append(f"{MODEL_LABELS[n]}\n(n={len(per)} of {m['test']['warning']['episodes_assessable']})")
    fig, ax = plt.subplots(figsize=(6, 3.8))
    if data:
        ax.boxplot(data, tick_labels=labels)
        ax.set_ylabel("warning time before onset (s)")
    else:
        ax.text(0.5, 0.5, "no episode was warned about by any model", ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("Prediction warning time (first alarm inside the horizon)")
    return _save(fig, out / "warning_times.png")


def plot_importance(importances: dict, out: Path):
    fig, axes = plt.subplots(1, len(importances), figsize=(5.5 * len(importances), 4.2), squeeze=False)
    for ax, (title, imp) in zip(axes[0], importances.items()):
        items = sorted(imp.items(), key=lambda kv: abs(kv[1]), reverse=True)[:12][::-1]
        ax.barh([k for k, _ in items], [v for _, v in items], color="#4c72b0")
        ax.set_title(title, fontsize=9)
    return _save(fig, out / "feature_importance.png")


def plot_timeline(df_run, score_by_model: dict, thresholds: dict, onsets_s: list, out: Path, title: str):
    fig, axes = plt.subplots(2, 1, figsize=(9, 5), sharex=True)
    t = df_run["t_rel_s"].to_numpy()
    axes[0].plot(t, df_run["rtt_mean_short_ms"], color="#333333")
    axes[0].set_ylabel("RTT, 1 s mean (ms)")
    for n, s in score_by_model.items():
        axes[1].plot(t, s, label=MODEL_LABELS[n], color=COLORS[n])
        axes[1].axhline(thresholds[n], color=COLORS[n], ls=":", lw=0.8)
    for ax in axes:
        for o in onsets_s:
            ax.axvline(o, color="orange", lw=1)
    axes[1].set_ylabel("predicted score"); axes[1].set_xlabel("time since first probe (s)"); axes[1].legend(fontsize=7)
    axes[0].set_title(title + "  (orange = degradation onset)", fontsize=9)
    return _save(fig, out / "example_timeline.png")
