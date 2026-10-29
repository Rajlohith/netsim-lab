"""Stage 7 analysis: everything here is computed from SAVED files only (campaign.json, plan.json, ledger.jsonl and each run
directory), so `analyze` can be repeated at any time, on another machine, without rerunning an experiment. All random procedures
(bootstrap) are seeded, therefore regenerated outputs are byte-identical apart from analysis_meta.json's timestamp.

Unit of analysis = one run (see stats.py). Handoff-level rows are exported (handoffs.csv) but never used for inference.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from ..netem.conditions import apply_conditions, direct_path_rtt_ms
from ..netem.topology_config import load_config
from ..traffic.clock import iso_utc, wall_ns
from . import stats as st
from .campaign import FACTORS, Ledger
from .experiment import ExperimentSpec

ALPHA = 0.05
CONF = 0.95
N_BOOT = 5000
MIN_PAIRS = 6          # same minimum as stats.verdict: fewer pairs can neither show a difference nor show that the emulator is deterministic
ANALYSIS_SEED = 20261028

# name -> (unit, kind, lower_is_better, cross_approach_testable, definition)
# kind: "observed" = timed/counted directly from the run's raw records; "derived" = computed with an assumption or a reference
#       value that is not itself a measurement of that run (topology-derived denominator, after-the-fact labelling, model output).
METRICS = {
    "app_stall_ms": ("ms", "observed", True, True,
                     "longest gap between consecutive echo arrivals in the window [handoff start - 250 ms, handoff end + 250 ms], mean over the run's handoffs (Stage 3 'max_interarrival_gap_in_window_ms'). Nominal probe spacing is 1000/rate_pps ms, so a no-stall value is that spacing."),
    "interruption_ms": ("ms", "observed", True, False,
                        "Stage 3 loss-burst interruption (time between the last echo before and the first after a loss burst); defined ONLY for handoffs where a probe-loss burst overlaps the window, so reliable transports (no probe loss) have no value. Mean over the run's handoffs that have one."),
    "handoff_latency_ms": ("ms", "observed", True, False,
                           "time from the handoff start to the approach's OWN recovery point (registration reply, binding update, first data on the new connection...). The recovery point differs per approach, so this is not compared across approaches."),
    "loss_percent": ("%", "observed", True, True,
                     "probes sent but never echoed, as a percentage of probes sent (whole run). Reliable transports (TCP, QUIC) retransmit, so their loss shows up as delay/stall instead: compare loss only within one transport class."),
    "handoff_loss_packets": ("packets", "observed", True, False,
                             "probes in the loss burst overlapping each handoff window, summed over the run's handoffs."),
    "handoffs_unrecovered": ("count", "observed", True, False,
                             "handoffs that never reached the approach's recovery point before the run ended."),
    "signalling_messages_per_handoff": ("messages", "observed", True, False,
                                        "mobility/transport signalling messages the mobile node sent or received between handoff start and recovery (approach_events.csv)."),
    "signalling_bytes_per_handoff": ("bytes", "observed", True, True,
                                     "payload bytes of those messages."),
    "mobility_overhead_bytes_per_packet": ("bytes", "observed", True, False,
                                           "extra header bytes the approach adds to each data packet. Counted from byte-accurate simulated packets; 'derived' when the run's overhead basis says analytic."),
    "rtt_after_ms": ("ms", "observed", True, False,
                     "median RTT of the first 10 echoes after the handoff window (Stage 3), mean over the run's handoffs."),
    "path_stretch": ("ratio", "derived", True, True,
                     "rtt_after_ms divided by the DIRECT-path RTT to the new access network. Numerator observed; denominator DERIVED from the run's effective link delays (2 x (cn-AN + AN-mn)). 1.0 = as short as the direct path; >1 = detour via the Home Agent or retransmission delay."),
}
TEST_METRICS = tuple(k for k, v in METRICS.items() if v[3])
PRED_COLUMNS = ("pred_status", "pred_decision_points", "pred_alarms", "pred_episodes", "pred_episodes_warned",
                "pred_false_alarm_decisions", "pred_warning_s_median", "pred_in_training_split")


def provenance_label(kind: str, data_origin: str | None) -> str:
    origin = {"simulated": "simulator-generated", "real-kernel-loopback": "measured on real kernel sockets (loopback)",
              "real-kernel-namespaces": "measured on real kernel namespaces"}.get(data_origin or "", "origin unknown")
    return f"{origin}; {kind}" if kind == "derived" else f"{origin}; directly observed in the raw records"


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _mean(v):
    v = [x for x in v if x is not None]
    return float(np.mean(v)) if v else None


def model_limit_note(approach: str, params: dict) -> str:
    if approach == "tcp-reconnect" and (params.get("loss_percent") or 0) > 0:
        return ("TcpLite (the emulator's TCP model) has no fast retransmit, SACK or congestion control: with random loss its "
                "stalls reflect that model, not the behaviour of a real TCP stack")
    return ""


# ---- collection -------------------------------------------------------------------------------------------------------------
def collect(results_dir) -> dict:
    """Read a campaign directory (or, without campaign.json, any directory of run folders) into run rows and handoff rows."""
    root = Path(results_dir)
    camp = _read_json(root / "campaign.json")
    plan = _read_json(root / "plan.json")
    if camp and plan:
        return _collect_campaign(root, camp, plan)
    return _collect_discovered(root)


def _collect_campaign(root: Path, camp: dict, plan: dict) -> dict:
    ledger = Ledger(root / "ledger.jsonl").final_status()
    runs, handoffs = [], []
    for pr in plan["runs"]:
        run_dir = root / "runs" / pr["scenario_id"] / pr["run_id"]
        led = ledger.get(pr["key"])
        exp = _read_json(run_dir / "experiment.json")
        if led:
            status, reason = led["status"], led["reason"]
        elif exp:
            status, reason = exp.get("status", "unknown"), exp.get("reason", "")
        else:
            status, reason = "not_run", "planned but never started"
        if status == "ok" and not (run_dir / "comparison_metrics.json").exists():
            status, reason = "incomplete", "ledger says ok but comparison_metrics.json is missing"
        row = _base_row(pr, status, reason, camp["config"]["name"])
        if status == "ok":
            m = _read_json(run_dir / "comparison_metrics.json")
            spec = ExperimentSpec.from_json(pr["spec"])
            hrows = _run_metrics(row, m, spec, pr)
            _attach_predictions(row, hrows, run_dir, ledger.get(pr["key"]))
            handoffs.extend(hrows)
        runs.append(row)
    return {"mode": "campaign", "campaign": camp, "runs": runs, "handoffs": handoffs,
            "cells": plan.get("not_executed_cells", []), "root": root}


def _collect_discovered(root: Path) -> dict:
    """Fallback for result folders produced before Stage 7 (e.g. `compare matrix` output): no design, no replicates."""
    runs, handoffs = [], []
    for ej in sorted(root.rglob("experiment.json")):
        exp = _read_json(ej)
        if not exp or "spec" not in exp:
            continue
        spec = ExperimentSpec.from_json(exp["spec"])
        run_dir = ej.parent
        pr = {"key": f"{spec.scenario_id}/{spec.run_id}", "scenario_id": spec.scenario_id, "run_id": spec.run_id, "condition": "unspecified",
              "factor": "unspecified", "level": None, "config_id": spec.scenario_id, "approach": spec.approach, "variant": spec.variant,
              "replicate": 0, "pair_key": spec.scenario_id, "run_seed": spec.seed, "condition_seed": spec.conditions.seed, "schedule": {}}
        row = _base_row(pr, exp.get("status", "unknown"), exp.get("reason", ""), root.name)
        m = _read_json(run_dir / "comparison_metrics.json")
        if row["status"] == "ok" and m:
            hrows = _run_metrics(row, m, spec, pr)
            _attach_predictions(row, hrows, run_dir, None)
            handoffs.extend(hrows)
        runs.append(row)
    return {"mode": "discovered", "campaign": None, "runs": runs, "handoffs": handoffs, "cells": [], "root": root}


def _base_row(pr, status, reason, campaign) -> dict:
    spec = pr.get("spec") or {}
    cond = spec.get("conditions") or {}
    row = {"campaign": campaign, "key": pr["key"], "scenario_id": pr["scenario_id"], "run_id": pr["run_id"], "condition": pr["condition"],
           "factor": pr["factor"], "level": pr["level"], "config_id": pr["config_id"], "approach": pr["approach"], "variant": pr["variant"],
           "replicate": pr["replicate"], "pair_key": pr["pair_key"], "status": status, "reason": reason,
           "run_seed": pr["run_seed"], "condition_seed": pr["condition_seed"], "data_origin": None}
    sched = pr.get("schedule") or {}
    row.update(first_move_s=sched.get("first_move_s"), second_move_s=sched.get("second_move_s"), dwell_s=sched.get("dwell_s"))
    if spec:
        row.update(p_latency_scale=cond.get("latency_scale"), p_loss_percent=cond.get("loss_percent"),
                   p_ha_distance_ms=cond.get("ha_distance_ms"), p_registration_loss_percent=cond.get("registration_loss_percent"),
                   p_ha_processing_ms=cond.get("ha_processing_ms"), p_detect_delay_ms=spec.get("detect_delay_ms"),
                   p_old_link_hold_ms=spec.get("old_link_hold_ms"), p_duration_s=spec.get("duration_s"), p_rate_pps=spec.get("rate_pps"),
                   p_payload=spec.get("payload"), p_environment=spec.get("environment"))
    return row


def _run_metrics(row: dict, m: dict, spec: ExperimentSpec, pr: dict) -> list:
    row["data_origin"] = (m.get("provenance") or {}).get("data_origin")
    row["p_latency_scale"] = spec.conditions.latency_scale
    row["p_loss_percent"] = spec.conditions.loss_percent
    row["p_ha_distance_ms"] = spec.conditions.ha_distance_ms
    row["p_registration_loss_percent"] = spec.conditions.registration_loss_percent
    row["p_ha_processing_ms"] = spec.conditions.ha_processing_ms
    row["p_detect_delay_ms"] = spec.detect_delay_ms
    row["p_old_link_hold_ms"] = spec.old_link_hold_ms
    row.update(p_duration_s=spec.duration_s, p_rate_pps=spec.rate_pps, p_payload=spec.payload, p_environment=spec.environment)
    cfg = apply_conditions(load_config(spec.topology), spec.conditions)
    hs = m.get("handoffs") or []
    hrows = []
    for h in hs:
        dest = (h.get("detail") or "").split("->")[-1].strip()
        ref = direct_path_rtt_ms(cfg, dest) if dest else None
        stretch = (h["rtt_after_ms"] / ref) if (h.get("rtt_after_ms") is not None and ref) else None
        hrows.append({"key": row["key"], "condition": row["condition"], "config_id": row["config_id"], "approach": row["approach"],
                      "replicate": row["replicate"], "event_id": h["event_id"], "detail": h.get("detail"),
                      "recovered": h["recovered"], "handoff_latency_ms": h["handoff_latency_ms"], "app_stall_ms": h["service_gap_ms"],
                      "interruption_ms": h["interruption_ms"], "loss_packets": h["loss_packets"], "rtt_before_ms": h["rtt_before_ms"],
                      "rtt_after_ms": h["rtt_after_ms"], "direct_rtt_reference_ms": ref, "path_stretch": stretch,
                      "signalling_messages": h["signalling_messages"], "signalling_bytes": h["signalling_bytes"]})
    n = len(hrows)
    ov = m.get("mobility_overhead") or {}
    row.update({
        "n_handoffs": n,
        "app_stall_ms": _mean([h["app_stall_ms"] for h in hrows]),
        "interruption_ms": _mean([h["interruption_ms"] for h in hrows]),
        "handoff_latency_ms": _mean([h["handoff_latency_ms"] for h in hrows]),
        "loss_percent": (m.get("loss") or {}).get("percent"),
        "lost_packets": (m.get("loss") or {}).get("lost"), "sent_packets": (m.get("loss") or {}).get("sent"),
        "handoff_loss_packets": sum(h["loss_packets"] or 0 for h in hrows) if hrows else None,
        "handoffs_unrecovered": sum(1 for h in hrows if not h["recovered"]) if hrows else None,
        "signalling_messages_per_handoff": (sum(h["signalling_messages"] for h in hrows) / n) if n else None,
        "signalling_bytes_per_handoff": (sum(h["signalling_bytes"] for h in hrows) / n) if n else None,
        "mobility_overhead_bytes_per_packet": ov.get("mobility_bytes_per_packet"),
        "overhead_basis": ov.get("basis"),
        "rtt_after_ms": _mean([h["rtt_after_ms"] for h in hrows]),
        "path_stretch": _mean([h["path_stretch"] for h in hrows]),
        "model_limit_note": model_limit_note(row["approach"], {"loss_percent": spec.conditions.loss_percent}),
    })
    return hrows


def _attach_predictions(row: dict, hrows: list, run_dir: Path, led) -> None:
    """Join the Stage 6 prediction outputs (handoff_predictions.csv/json written next to the raw data) with the run's
    measured outcomes. The predictions were made by replay with only observations <= each decision time; the outcomes (episodes)
    are the run's own measured degradation, labelled AFTER the fact with the Stage 6 label rule, so they are 'derived'."""
    for c in PRED_COLUMNS:
        row[c] = None
    pj = _read_json(run_dir / "handoff_predictions.json")
    pc = run_dir / "handoff_predictions.csv"
    if pj is None or not pc.exists():
        row["pred_status"] = "failed" if (led and (led.get("prediction") or {}).get("ok") is False) else "none"
        for h in hrows:
            h.update(pred_warned=None, pred_warning_s=None, pred_points_in_horizon=None)
        return
    with open(pc, newline="") as f:
        preds = list(csv.DictReader(f))
    horizon_ns = int(round(float(preds[0]["horizon_s"]) * 1e9)) if preds else 0
    eps = pj.get("ground_truth_episodes", [])
    onsets = [e["onset_ns"] for e in eps]
    false_alarms = 0
    for p in preds:
        if int(p["alarm"]) and not any(0 < o - int(p["decision_time_ns"]) <= horizon_ns for o in onsets):
            false_alarms += 1
    by_event = {e["event_id"]: e for e in eps}
    for h in hrows:
        e = by_event.get(h["event_id"])
        h.update(pred_warned=(e["warned"] if e else None), pred_warning_s=(e["warning_s"] if e else None),
                 pred_points_in_horizon=(e["decision_points_in_horizon"] if e else None))
    warn = [e["warning_s"] for e in eps if e["warned"] and e["warning_s"] is not None]
    row.update(pred_status="ok", pred_decision_points=pj["decision_points"], pred_alarms=pj["alarms"], pred_episodes=len(eps),
               pred_episodes_warned=sum(1 for e in eps if e["warned"]), pred_false_alarm_decisions=false_alarms,
               pred_warning_s_median=float(np.median(warn)) if warn else None, pred_in_training_split=pj.get("run_in_training_split"))


# ---- aggregation ------------------------------------------------------------------------------------------------------------
def _cell_key(r):
    return (r["condition"], r["config_id"])


def summarise(data: dict) -> list:
    """One row per (condition, approach configuration, metric): n and distribution of the run-level values (ok runs only), with the
    counts of runs that did not finish so that survivorship is visible."""
    runs = data["runs"]
    by_cell = defaultdict(list)
    for r in runs:
        by_cell[_cell_key(r)].append(r)
    rows = []
    for (cond, cfg_id), rs in sorted(by_cell.items()):
        ok = [r for r in rs if r["status"] == "ok"]
        counts = defaultdict(int)
        for r in rs:
            counts[r["status"]] += 1
        for metric, (unit, kind, lower, testable, _d) in METRICS.items():
            vals = [r.get(metric) for r in ok]
            origin = next((r["data_origin"] for r in ok if r["data_origin"]), None)
            basis_analytic = metric == "mobility_overhead_bytes_per_packet" and any(str(r.get("overhead_basis") or "").startswith("analytic") for r in ok)
            d = st.describe(vals, CONF)
            rows.append({"condition": cond, "factor": rs[0]["factor"], "level": rs[0]["level"], "config_id": cfg_id,
                         "approach": rs[0]["approach"], "metric": metric, "unit": unit,
                         "provenance": provenance_label("derived" if (kind == "derived" or basis_analytic) else "observed", origin),
                         "planned_runs": len(rs), "ok_runs": len(ok), "failed_runs": counts["failed"] + counts["invalid"],
                         "incomplete_runs": counts["incomplete"] + counts["interrupted"], "not_run": counts["not_run"],
                         "runs_with_value": d["n"], **{k: d.get(k) for k in ("min", "q1", "median", "q3", "max", "mean", "sd",
                                                                           "median_ci_lo", "median_ci_hi", "median_ci_coverage",
                                                                           "mean_ci_lo", "mean_ci_hi", "degenerate", "ci_note")},
                         "caveat": next((r["model_limit_note"] for r in ok if r.get("model_limit_note")), "")})
    return rows


def _series(runs, cond, cfg_id, metric):
    return {r["replicate"]: r.get(metric) for r in runs if r["condition"] == cond and r["config_id"] == cfg_id and r["status"] == "ok"}


def _constant(series: dict) -> bool:
    """True when every replicate produced the same value: the emulator did not vary, so there is nothing to test statistically."""
    v = {x for x in series.values() if x is not None}
    return len(v) == 1


def _paired(sa: dict, sb: dict, reps, label_note: str = ""):
    """(PairedResult, deterministic?) for two replicate-indexed series. When BOTH series are constant the difference is a
    property of the deterministic design, not a statistical finding: no p-value is produced and it is classified separately."""
    res = st.paired_compare([sa.get(k) for k in reps], [sb.get(k) for k in reps], CONF, N_BOOT, ANALYSIS_SEED)
    det = res.n_pairs >= MIN_PAIRS and _constant(sa) and _constant(sb)     # one or two identical values prove nothing about determinism
    if det:
        res.p_value = None
        res.note = "both series are identical in every replicate: deterministic difference, statistical test not applicable"
    return res, det


def _verdict(res, padj, det, a_name="a lower", b_name="b lower"):
    if det:
        return "identical in every replicate" if res.median_diff == 0 else "deterministic difference"
    return st.verdict(res, padj, ALPHA)


def compare_approaches(data: dict) -> list:
    """Matched-pairs comparison of every pair of approach configurations within each condition, for the cross-approach metrics.
    Holm adjustment within each (condition, metric) family."""
    runs = data["runs"]
    conds = sorted({r["condition"] for r in runs})
    out = []
    for cond in conds:
        cfgs = sorted({r["config_id"] for r in runs if r["condition"] == cond and r["status"] == "ok"})
        reps = sorted({r["replicate"] for r in runs if r["condition"] == cond})
        for metric in TEST_METRICS:
            fam = []
            for i, a in enumerate(cfgs):
                for b in cfgs[i + 1:]:
                    sa, sb = _series(runs, cond, a, metric), _series(runs, cond, b, metric)
                    res, det = _paired(sa, sb, reps)
                    fam.append((a, b, res, det))
            padj = st.holm([r.p_value for _a, _b, r, _d in fam])
            for (a, b, res, det), pa in zip(fam, padj):
                caveats = []
                ra = next((r for r in runs if r["condition"] == cond and r["config_id"] == a and r["status"] == "ok"), None)
                rb = next((r for r in runs if r["condition"] == cond and r["config_id"] == b and r["status"] == "ok"), None)
                for x in (ra, rb):
                    if x and x.get("model_limit_note"):
                        caveats.append(f"{x['config_id']}: {x['model_limit_note']}")
                if metric == "loss_percent":
                    caveats.append("reliable transports (tcp-reconnect, quic-migration) retransmit, so their probe loss is not comparable with UDP-based approaches")
                out.append({"condition": cond, "metric": metric, "a": a, "b": b, **res.to_dict(), "p_holm": pa, "alpha": ALPHA,
                            "deterministic": det, "verdict": _verdict(res, pa, det), "caveat": "; ".join(dict.fromkeys(caveats))})
    return out


def compare_to_baseline(data: dict) -> list:
    """Within one approach configuration: each non-baseline condition against the baseline, matched by replicate. Holm within
    each (configuration, metric) family. Answers 'does this condition change the metric?'."""
    runs = data["runs"]
    cfgs = sorted({r["config_id"] for r in runs})
    out = []
    for cfg_id in cfgs:
        conds = sorted({r["condition"] for r in runs if r["config_id"] == cfg_id and r["condition"] != "baseline"})
        reps = sorted({r["replicate"] for r in runs if r["config_id"] == cfg_id})
        if "baseline" not in {r["condition"] for r in runs if r["config_id"] == cfg_id}:
            continue
        for metric in TEST_METRICS + ("handoff_latency_ms", "handoffs_unrecovered"):
            base = _series(runs, "baseline", cfg_id, metric)
            fam = []
            for cond in conds:
                s = _series(runs, cond, cfg_id, metric)
                if not s:
                    continue
                res, det = _paired(s, base, reps)
                fam.append((cond, res, det))
            padj = st.holm([r.p_value for _c, r, _d in fam])
            for (cond, res, det), pa in zip(fam, padj):
                v = _verdict(res, pa, det)
                out.append({"config_id": cfg_id, "condition": cond, "metric": metric, **res.to_dict(), "p_holm": pa, "alpha": ALPHA,
                            "deterministic": det,
                            "verdict": {"a lower": "condition lower than baseline", "b lower": "condition higher than baseline",
                                        "deterministic difference": "deterministic change vs baseline",
                                        "identical in every replicate": "identical to baseline"}.get(v, v)})
    return out


def prediction_summary(data: dict) -> list:
    """Per (condition, configuration): how often the Stage 6 model warned before measured degradation episodes. Pooled counts are
    descriptive; the CI resamples RUNS (episodes inside a run are not independent)."""
    runs = [r for r in data["runs"] if r["status"] == "ok" and r.get("pred_status") == "ok"]
    by = defaultdict(list)
    for r in runs:
        by[_cell_key(r)].append(r)
    out = []
    for (cond, cfg_id), rs in sorted(by.items()):
        ep = np.array([r["pred_episodes"] for r in rs], dtype=float)
        wn = np.array([r["pred_episodes_warned"] for r in rs], dtype=float)
        fa = np.array([r["pred_false_alarm_decisions"] for r in rs], dtype=float)
        dp = np.array([r["pred_decision_points"] for r in rs], dtype=float)
        ci = st.cluster_bootstrap_ratio(wn, ep, CONF, N_BOOT, ANALYSIS_SEED) if ep.sum() > 0 and len(rs) >= 2 else None
        out.append({"condition": cond, "config_id": cfg_id, "runs_with_predictions": len(rs),
                    "episodes_total": int(ep.sum()), "episodes_warned": int(wn.sum()),
                    "warned_fraction": float(wn.sum() / ep.sum()) if ep.sum() else None,
                    "warned_fraction_ci_lo": ci[0] if ci else None, "warned_fraction_ci_hi": ci[1] if ci else None,
                    "false_alarm_decisions_per_100_decisions": float(100 * fa.sum() / dp.sum()) if dp.sum() else None,
                    "runs_in_model_training_split": int(sum(1 for r in rs if r.get("pred_in_training_split"))),
                    "note": "predictions are replayed from observations <= each decision time; episodes are labelled after the fact from the same run (derived); "
                            "simulated data only, not evidence of skill on real networks"})
    return out


# ---- export ---------------------------------------------------------------------------------------------------------------
def _fmt(v):
    if isinstance(v, float):
        return repr(round(v, 9))
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (np.integer,)):
        return str(int(v))
    if isinstance(v, (np.floating,)):
        return repr(round(float(v), 9))
    return v


def write_csv(path: Path, rows: list, columns: list | None = None):
    cols = columns or list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in cols])


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if math.isnan(float(o)) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    raise TypeError(f"not JSON serialisable: {type(o)}")


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, default=_json_default, allow_nan=False) + "\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyze(results_dir, out_dir=None) -> dict:
    """Recompute every analysis output from the saved results and write them to out_dir (default <results_dir>/analysis)."""
    root = Path(results_dir)
    out = Path(out_dir) if out_dir else root / "analysis"
    out.mkdir(parents=True, exist_ok=True)
    data = collect(root)
    runs, handoffs = data["runs"], data["handoffs"]
    summary = summarise(data)
    comps = compare_approaches(data)
    vsb = compare_to_baseline(data)
    preds = prediction_summary(data)
    write_csv(out / "runs.csv", runs)
    write_json(out / "runs.json", runs)
    write_csv(out / "handoffs.csv", handoffs)
    write_csv(out / "summary.csv", summary)
    write_json(out / "summary.json", summary)
    write_csv(out / "comparisons.csv", comps)
    write_json(out / "comparisons.json", comps)
    write_csv(out / "vs_baseline.csv", vsb)
    write_json(out / "vs_baseline.json", vsb)
    write_csv(out / "predictions_summary.csv", preds)
    write_json(out / "predictions_summary.json", preds)
    write_csv(out / "not_executed_cells.csv", data["cells"])
    write_json(out / "metric_definitions.json", {k: {"unit": v[0], "kind": v[1], "lower_is_better": v[2],
                                                    "tested_across_approaches": v[3], "definition": v[4]} for k, v in METRICS.items()})
    status_counts = defaultdict(int)
    for r in runs:
        status_counts[r["status"]] += 1
    meta = {"schema_version": 1, "results_dir": root.name, "mode": data["mode"], "generated_wall_iso": iso_utc(wall_ns()),
            "campaign": (data["campaign"] or {}).get("config"), "config_sha256": (data["campaign"] or {}).get("config_sha256"),
            "run_status_counts": dict(sorted(status_counts.items())), "handoff_rows": len(handoffs),
            "methods": {"confidence_level": CONF, "alpha": ALPHA, "bootstrap_resamples": N_BOOT, "bootstrap_seed": ANALYSIS_SEED,
                        "median_ci": "exact distribution-free (order statistics, binomial)", "mean_ci": "Student t",
                        "paired_test": "Wilcoxon signed-rank (two-sided), matched by replicate seed",
                        "effect": "median of paired differences with percentile-bootstrap CI over pairs",
                        "multiplicity": "Holm-Bonferroni within each (condition, metric) family [approach pairs] or (configuration, metric) family [vs baseline]",
                        "unit_of_analysis": "one run (own seed); per-handoff values are averaged within a run before inference"},
            "inputs": {n: _sha(root / n) for n in ("campaign.json", "plan.json", "ledger.jsonl") if (root / n).exists()}}
    write_json(out / "analysis_meta.json", meta)
    (out / "REPORT.md").write_text(render_report(data, summary, comps, vsb, preds, meta))
    return {"out_dir": str(out), "runs": len(runs), "status_counts": dict(status_counts), "summary_rows": len(summary),
            "comparisons": len(comps), "vs_baseline": len(vsb), "prediction_cells": len(preds)}


# ---- report ---------------------------------------------------------------------------------------------------------------
def _n(v, nd=1):
    return "n/a" if v is None else f"{v:.{nd}f}"


def render_report(data, summary, comps, vsb, preds, meta) -> str:
    cfg = (data["campaign"] or {}).get("config") or {}
    L = ["# Experiment report (generated by `python -m app.compare analyze`)", ""]
    L += [f"* campaign: `{cfg.get('name', 'n/a')}` ({data['mode']} mode); replicates per cell: {cfg.get('replicates', 'n/a')}; "
          f"base seed: {cfg.get('base_seed', 'n/a')}; run duration {cfg.get('duration_s', 'n/a')} s; probes {cfg.get('rate_pps', 'n/a')} pps",
          f"* runs by status: {meta['run_status_counts']}",
          "* **All values in this report come from the deterministic virtual-time emulator (simulator-generated).** They describe "
          "the simulator under the stated conditions, not real networks. 'derived' values use a reference or a label that is not itself a "
          "measurement of that run (see metric_definitions.json).",
          "* Replicates are independent runs with their own seed; handoffs inside a run are averaged first. Comparisons are matched pairs "
          "(same replicate seed = same handoff schedule and loss seed in every approach).", ""]
    if data["mode"] == "discovered":
        L += ["> No campaign design was found: runs were discovered in the folder, there is one run per scenario and therefore NO "
              "confidence intervals or tests. Run a campaign to get them.", ""]
    ok = [s for s in summary if s["ok_runs"]]
    if cfg and any(s["failed_runs"] or s["incomplete_runs"] or s["not_run"] for s in summary):
        bad = sorted({(s["condition"], s["config_id"]) for s in summary if s["failed_runs"] or s["incomplete_runs"] or s["not_run"]})
        L += ["## Runs that did not finish", "", "Statistics below are computed over finished runs only; these cells lost runs:", ""]
        L += [f"* `{c}` / `{a}`" for c, a in bad[:40]] + [""]
    L += ["## Baseline conditions: median [95% CI] per approach configuration", ""]
    for metric in ("app_stall_ms", "loss_percent", "handoff_latency_ms", "path_stretch", "signalling_bytes_per_handoff",
                   "mobility_overhead_bytes_per_packet"):
        rows = [s for s in ok if s["condition"] in ("baseline", "unspecified") and s["metric"] == metric]
        if not rows:
            continue
        unit, kind, _l, _t, definition = METRICS[metric]
        L += [f"### {metric} ({unit}) -- {rows[0]['provenance']}", "", f"_{definition}_", "",
              "| configuration | n runs | median | 95% CI of median | min..max | note |", "|---|---|---|---|---|---|"]
        for s in rows:
            ci = "n/a" if s["median_ci_lo"] is None else f"[{_n(s['median_ci_lo'], 2)}, {_n(s['median_ci_hi'], 2)}]"
            note = s["ci_note"] or s["caveat"]
            L.append(f"| `{s['config_id']}` | {s['runs_with_value']} | {_n(s['median'], 2)} | {ci} | {_n(s['min'], 2)}..{_n(s['max'], 2)} | {note} |")
        L.append("")
    distinguishable = [c for c in comps if c["verdict"] in ("a lower", "b lower")]
    det = [c for c in comps if c.get("deterministic")]
    L += ["## Statistical differences between approaches (matched pairs, Holm-corrected)", "",
          "A row appears only if the Holm-adjusted p-value is below 0.05 AND the bootstrap CI of the paired median difference excludes 0 "
          "AND at least one of the two series varies between replicates. "
          "'Lower' refers to ONE metric under ONE simulated condition; it is not an overall ranking.", ""]
    if not distinguishable:
        L += ["_None._", ""]
    else:
        L += ["| condition | metric | lower | higher | median difference (lower minus higher) | 95% CI | Holm p | caveat |", "|---|---|---|---|---|---|---|---|"]
        for c in distinguishable:
            lo, hi = (c["a"], c["b"]) if c["verdict"] == "a lower" else (c["b"], c["a"])
            md = c["median_diff"] if c["verdict"] == "a lower" else -c["median_diff"]
            ci = (c["diff_ci_lo"], c["diff_ci_hi"]) if c["verdict"] == "a lower" else (-c["diff_ci_hi"], -c["diff_ci_lo"])
            L.append(f"| {c['condition']} | {c['metric']} | `{lo}` | `{hi}` | {_n(md, 3)} | [{_n(ci[0], 3)}, {_n(ci[1], 3)}] | {c['p_holm']:.3g} | {c['caveat']} |")
        L.append("")
    not_dist = [c for c in comps if c["verdict"] == "no distinguishable difference"]
    L += [f"Comparisons without a distinguishable difference: {len(not_dist)}; with insufficient data: "
          f"{sum(1 for c in comps if c['verdict'] == 'insufficient data')}; deterministic (see below): {len(det)}; total comparisons: {len(comps)}.", ""]
    L += ["## Deterministic differences (no variability in either series)", "",
          "In these comparisons both approaches gave the SAME value in every replicate (the emulator is deterministic for that metric "
          "and condition). The difference is exact for this simulator and design but is not a statistical result: no p-value is computed. "
          "Baseline condition shown; every condition is in comparisons.csv.", ""]
    bd = [c for c in det if c["condition"] in ("baseline", "unspecified") and c["verdict"] == "deterministic difference"]
    if bd:
        L += ["| metric | a | b | a minus b |", "|---|---|---|---|"]
        L += [f"| {c['metric']} | `{c['a']}` | `{c['b']}` | {_n(c['median_diff'], 3)} |" for c in bd]
        L.append("")
    else:
        L += ["_None at baseline._", ""]
    L += ["## Effect of each condition against the baseline (same approach, matched by replicate)", ""]
    changed = [v for v in vsb if v["verdict"] in ("condition lower than baseline", "condition higher than baseline", "deterministic change vs baseline")]
    if not changed:
        L += ["_No condition produced a distinguishable change._", ""]
    else:
        L += ["Rows marked *deterministic* have no variability in either series (exact for this simulator, no p-value).", "",
              "| configuration | condition | metric | direction | median change | 95% CI | Holm p |", "|---|---|---|---|---|---|---|"]
        for v in changed:
            ci = "deterministic" if v["deterministic"] else f"[{_n(v['diff_ci_lo'], 3)}, {_n(v['diff_ci_hi'], 3)}]"
            p = "n/a" if v["p_holm"] is None else f"{v['p_holm']:.3g}"
            L.append(f"| `{v['config_id']}` | {v['condition']} | {v['metric']} | {v['verdict']} | {_n(v['median_diff'], 3)} | {ci} | {p} |")
        L.append("")
    if preds:
        L += ["## Handoff-degradation predictions (Stage 6 model) next to the measurements", "",
              "| condition | configuration | runs | episodes | warned | warned fraction [95% CI over runs] | false-alarm decisions per 100 |",
              "|---|---|---|---|---|---|---|"]
        for p in preds:
            ci = "n/a" if p["warned_fraction_ci_lo"] is None else f"[{p['warned_fraction_ci_lo']:.2f}, {p['warned_fraction_ci_hi']:.2f}]"
            L.append(f"| {p['condition']} | `{p['config_id']}` | {p['runs_with_predictions']} | {p['episodes_total']} | {p['episodes_warned']} | "
                     f"{_n(p['warned_fraction'], 2)} {ci} | {_n(p['false_alarm_decisions_per_100_decisions'], 2)} |")
        L += ["", "Predictions are replayed from observations up to each decision time only; episodes are labelled afterwards from the same run. "
              "The Stage 6 model was trained on generator regularities of simulated data; nothing here shows skill on real networks.", ""]
    if data["cells"]:
        L += ["## Cells that were not executed (reported, not skipped)", "", "| condition | configuration | status | reason |", "|---|---|---|---|"]
        for c in data["cells"]:
            L.append(f"| {c['condition']} | `{c['config_id']}` | {c['status']} | {c['reason']} |")
        L.append("")
    L += ["## Limitations", "",
          "* One-factor-at-a-time design: interactions between conditions are not estimated.",
          "* The emulator has fixed link delays (no jitter, queues or bandwidth limits); loss is independent per packet and direction.",
          "* Handoff timing varies only by the seeded schedule draw; all replicates share the same topology and probe workload.",
          "* TcpLite is a simplified model (no fast retransmit/SACK/congestion control); the QUIC adapter was not runnable here (see not-executed cells).",
          "* Confidence intervals describe sampling variability of the simulator under the stated design; they say nothing about real-world variability.", ""]
    return "\n".join(L)
