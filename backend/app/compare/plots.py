"""Stage 7 charts, drawn from the saved analysis outputs (runs.csv / summary.json / predictions_summary.json), never from a live
simulation: `python -m app.compare plots <results_dir>` can be rerun at any time. matplotlib with the Agg backend (no display)."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .analysis import METRICS  # noqa: E402
from .campaign import FACTORS  # noqa: E402

PARAM = {"latency_scale": "p_latency_scale", "loss_percent": "p_loss_percent", "ha_distance_ms": "p_ha_distance_ms",
         "detect_delay_ms": "p_detect_delay_ms", "dwell_s": "dwell_s", "registration_loss_percent": "p_registration_loss_percent",
         "ha_processing_ms": "p_ha_processing_ms", "old_link_hold_ms": "p_old_link_hold_ms"}
FOOT = "simulator-generated values (deterministic virtual-time emulator); error bars/bands: exact 95% CI of the median where n >= 6"


def _load(analysis_dir: Path):
    with open(analysis_dir / "runs.csv", newline="") as f:
        runs = list(csv.DictReader(f))
    summary = json.loads((analysis_dir / "summary.json").read_text())
    preds = json.loads((analysis_dir / "predictions_summary.json").read_text())
    return runs, summary, preds


def _f(x):
    return None if x in (None, "") else float(x)


def _fig(path: Path, fig):
    fig.text(0.01, -0.04, FOOT, fontsize=6, color="#555")
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)


def baseline_distribution(runs, metric, out: Path):
    by = defaultdict(list)
    for r in runs:
        if r["status"] == "ok" and r["condition"] in ("baseline", "unspecified") and _f(r.get(metric)) is not None:
            by[r["config_id"]].append(_f(r[metric]))
    if not by:
        return None
    names = sorted(by)
    fig, ax = plt.subplots(figsize=(1.6 * len(names) + 2, 4))
    ax.boxplot([by[n] for n in names], tick_labels=names, showmeans=False)
    for i, n in enumerate(names, 1):
        xs = [i + (j % 7 - 3) * 0.04 for j in range(len(by[n]))]
        ax.scatter(xs, by[n], s=10, alpha=0.6, color="#1f77b4", zorder=3)
    ax.set_ylabel(f"{metric} ({METRICS[metric][0]})")
    ax.set_title(f"{metric} per run, baseline condition (each dot = one independent run)")
    ax.tick_params(axis="x", rotation=25)
    p = out / f"baseline_{metric}.png"
    _fig(p, fig)
    return p


def sweep_plot(runs, summary, factor, metric, out: Path):
    rows = [s for s in summary if s["metric"] == metric and s["ok_runs"] and s["median"] is not None and s["factor"] in ("baseline", factor)]
    if not any(s["factor"] == factor for s in rows):
        return None
    xval = {}
    for r in runs:                                   # actual parameter value of each condition, from the run records
        if r["status"] == "ok" and r["condition"] not in xval and (r["factor"] == factor or r["factor"] == "baseline"):
            xval[r["condition"]] = _f(r.get(PARAM[factor]))
    series = defaultdict(list)
    for s in rows:
        x = xval.get(s["condition"])
        if x is None:
            continue
        series[s["config_id"]].append((x, s["median"], s["median_ci_lo"], s["median_ci_hi"]))
    series = {k: sorted(v) for k, v in series.items() if len(v) >= 2}
    if not series:
        return None
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for name, pts in sorted(series.items()):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, marker="o", label=name)
        lo = [p[2] if p[2] is not None else p[1] for p in pts]
        hi = [p[3] if p[3] is not None else p[1] for p in pts]
        ax.fill_between(xs, lo, hi, alpha=0.15)
    ax.set_xlabel(factor)
    ax.set_ylabel(f"median {metric} ({METRICS[metric][0]})")
    ax.set_title(f"{metric} vs {factor}")
    ax.legend(fontsize=7)
    ax.grid(alpha=0.3)
    if factor == "loss_percent" and any(n.startswith("tcp-reconnect") for n in series):
        ax.text(0.01, 0.98, "tcp-reconnect curves under loss describe TcpLite (no fast retransmit/SACK), not real TCP",
                transform=ax.transAxes, fontsize=6.5, va="top", color="#a33")
    p = out / f"sweep_{factor}_{metric}.png"
    _fig(p, fig)
    return p


def overhead_chart(summary, out: Path):
    rows = {}
    for s in summary:
        if s["condition"] in ("baseline", "unspecified") and s["ok_runs"] and s["median"] is not None:
            rows.setdefault(s["config_id"], {})[s["metric"]] = s["median"]
    names = sorted(n for n, d in rows.items() if "mobility_overhead_bytes_per_packet" in d)
    if not names:
        return None
    fig, axs = plt.subplots(1, 2, figsize=(9, 3.8))
    axs[0].bar(names, [rows[n].get("mobility_overhead_bytes_per_packet") or 0 for n in names], color="#2ca02c")
    axs[0].set_title("header overhead per data packet (bytes)")
    axs[1].bar(names, [rows[n].get("signalling_bytes_per_handoff") or 0 for n in names], color="#d62728")
    axs[1].set_title("signalling payload per handoff (bytes)")
    for a in axs:
        a.tick_params(axis="x", rotation=30, labelsize=7)
    p = out / "baseline_overhead.png"
    _fig(p, fig)
    return p


def prediction_chart(preds, out: Path):
    rows = [p for p in preds if p["warned_fraction"] is not None and p["condition"] in ("baseline", "unspecified")]
    if not rows:
        return None
    fig, ax = plt.subplots(figsize=(7, 3.8))
    names = [r["config_id"] for r in rows]
    vals = [r["warned_fraction"] for r in rows]
    err = [[v - (r["warned_fraction_ci_lo"] if r["warned_fraction_ci_lo"] is not None else v) for v, r in zip(vals, rows)],
           [(r["warned_fraction_ci_hi"] if r["warned_fraction_ci_hi"] is not None else v) - v for v, r in zip(vals, rows)]]
    ax.bar(names, vals, yerr=err, capsize=3, color="#9467bd")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("fraction of measured degradation episodes warned in advance")
    ax.set_title("Stage 6 predictions vs measured episodes (baseline, 95% CI over runs)")
    ax.tick_params(axis="x", rotation=25, labelsize=7)
    p = out / "baseline_prediction_warned_fraction.png"
    _fig(p, fig)
    return p


def make_plots(results_dir, out_dir=None) -> list:
    root = Path(results_dir)
    adir = root / "analysis"
    if not (adir / "runs.csv").exists():
        raise FileNotFoundError(f"{adir}/runs.csv not found: run `python -m app.compare analyze {root}` first")
    out = Path(out_dir) if out_dir else adir / "plots"
    out.mkdir(parents=True, exist_ok=True)
    runs, summary, preds = _load(adir)
    made = []
    for m in ("app_stall_ms", "loss_percent", "handoff_latency_ms", "path_stretch"):
        made.append(baseline_distribution(runs, m, out))
    made.append(overhead_chart(summary, out))
    for f in FACTORS:
        for m in ("app_stall_ms", "loss_percent", "path_stretch"):
            made.append(sweep_plot(runs, summary, f, m, out))
    made.append(prediction_chart(preds, out))
    return [str(p.relative_to(root)) if p.is_relative_to(root) else str(p) for p in made if p]
