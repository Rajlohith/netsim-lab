"""One normaliser for every approach: probe metrics (Stage 3) + approach_events.csv + experiment_summary.json
-> comparison_metrics.json with the SAME core metrics. Values that cannot be measured are null with a reason."""
from __future__ import annotations

import json
from pathlib import Path

from ..traffic.analyze import analyze_run
from ..traffic.loader import read_csv, read_json
from . import events as ev

SCHEMA_VERSION = 1


def _ms(a, b):
    return None if a is None or b is None else round((b - a) / 1e6, 6)


def normalize(run_dir, write=True) -> dict:
    d = Path(run_dir)
    m = analyze_run(d)
    rows = ev.read_events(d / "approach_events.csv") or []
    summary = read_json(d / "experiment_summary.json") or {}
    hs = [r for r in read_csv(d / "events.csv", "events.csv").rows if r["kind"] == "handoff_start"]
    hs.sort(key=lambda r: r["mono_ns"])
    stage3 = {h["event_id"]: h for h in (m.get("handoffs") or [])}
    out = []
    for i, s in enumerate(hs):
        start = s["mono_ns"]
        lim = hs[i + 1]["mono_ns"] if i + 1 < len(hs) else float("inf")
        rec = next((e for e in rows if e["kind"] == "recovered" and start <= e["mono_ns"] < lim), None)
        upto = rec["mono_ns"] if rec else lim
        sig = [e for e in rows if e["kind"] in ("signal_tx", "signal_rx") and start <= e["mono_ns"] <= upto]
        h3 = stage3.get(s["event_id"])
        burst = (h3 or {}).get("loss_burst")
        out.append({
            "event_id": s["event_id"], "detail": s["detail"], "recovered": rec is not None,
            "handoff_latency_ms": _ms(start, rec["mono_ns"]) if rec else None,
            "service_gap_ms": (h3 or {}).get("max_interarrival_gap_in_window_ms"),
            "interruption_ms": (h3 or {}).get("interruption_ms"),
            "loss_packets": (burst["packets"] if burst else 0) if h3 else None,
            "rtt_before_ms": (h3 or {}).get("rtt_median_before_ms"),
            "rtt_after_ms": (h3 or {}).get("rtt_median_after_ms"),
            "signalling_messages": len(sig), "signalling_bytes": sum(e["bytes"] for e in sig),
            "retransmits": sum(1 for e in rows if e["kind"] == "retransmit" and start <= e["mono_ns"] < lim),
            "failures": [e["detail"] for e in rows if e["kind"] == "failure" and start <= e["mono_ns"] < lim],
        })

    def mean(key):
        v = [h[key] for h in out if h[key] is not None]
        return round(sum(v) / len(v), 6) if v else None

    def mx(key):
        v = [h[key] for h in out if h[key] is not None]
        return max(v) if v else None

    L, rtt = m["loss"], m.get("rtt_ms") or {}
    failures = [e["detail"] for e in rows if e["kind"] == "failure"]
    cont = dict(summary.get("continuity", {}))
    cont.update(all_handoffs_recovered=all(h["recovered"] for h in out) if out else None,
                data_loss_free=(L["lost"] == 0) if L["lost"] is not None else None,
                data_loss_free_exact=L["exact"], failure_events=len(failures))
    mode = summary.get("mode") or ""
    ov_basis = ((summary.get("overhead") or {}).get("basis")) or ""
    origin = ("simulated" if mode.startswith("emulated") else "real-kernel-loopback" if mode.startswith("loopback")
              else "real-kernel-namespaces" if mode.startswith("live") else "unknown")
    provenance = {
        "data_origin": origin,
        "clock": "simulated virtual time (deterministic, no real delays)" if origin == "simulated" else "real monotonic clock",
        "network": {"simulated": "app.mobileip.simnet discrete-event emulator (no queues, bandwidth or kernel stack)",
                    "real-kernel-loopback": "real kernel TCP over loopback; path change emulated by blackholing a socket; no path delay",
                    "real-kernel-namespaces": "real Linux namespaces/tunnels (written, unverified in the development sandbox)",
                    "unknown": "unknown"}[origin],
        "protocol_engine": (summary.get("model") or {}).get("transport") or (summary.get("model") or {}).get("protocol"),
        "analytic_values": [k for k, v in (("mobility_overhead", ov_basis),) if v.startswith("analytic")],
        "timing_comparable_with_other_environments": origin == "simulated",
    }
    res = {
        "schema_version": SCHEMA_VERSION, "provenance": provenance, "approach": summary.get("approach"), "environment": summary.get("environment"),
        "scenario_id": m["scenario_id"], "run_id": m["run_id"], "run_status": m["run_status"],
        "timing_comparable": summary.get("timing_comparable", True), "mode": summary.get("mode"),
        "handoffs": out,
        "handoff_summary": {"count": len(out), "handoff_latency_ms_mean": mean("handoff_latency_ms"),
                            "handoff_latency_ms_max": mx("handoff_latency_ms"),
                            "service_gap_ms_mean": mean("service_gap_ms"), "service_gap_ms_max": mx("service_gap_ms"),
                            "loss_packets_total": sum(h["loss_packets"] or 0 for h in out),
                            "signalling_messages_total": sum(h["signalling_messages"] for h in out),
                            "signalling_bytes_total": sum(h["signalling_bytes"] for h in out)},
        "loss": {"lost": L["lost"], "sent": L["sent_ok"], "percent": L["loss_percent"], "exact": L["exact"]},
        "duplicates": m["duplicates"], "reordered_packets": m["reordered_packets"],
        "rtt_ms": {k: rtt.get(k) for k in ("count", "min", "p50", "p95", "max")},
        "goodput_bps": m["throughput"]["goodput_bps"],
        "mobility_overhead": summary.get("overhead"),
        "continuity": cont,
        "failures": failures, "warnings": m["warnings"], "model": summary.get("model"),
        "handoffs_unavailable_reason": m.get("handoffs_unavailable_reason"),
    }
    if write:
        (d / "comparison_metrics.json").write_text(json.dumps(res, indent=2, sort_keys=True))
    return res
