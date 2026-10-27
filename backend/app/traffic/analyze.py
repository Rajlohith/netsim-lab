"""Load a run directory, compute metrics, write metrics.json."""
from __future__ import annotations

import json
from pathlib import Path

from .loader import read_csv, read_json
from .metrics import compute_metrics


def analyze_run(run_dir, *, session_id=None, bin_s=1.0, handoff_margin_ms=250.0, write=True) -> dict:
    d = Path(run_dir)
    notes = []
    loaded = {}
    for name in ("sent.csv", "echoes.csv", "received.csv", "events.csv"):
        r = read_csv(d / name, name)
        loaded[name] = r
        if r.truncated_tail:
            notes.append(f"{name}: unterminated last line dropped (partial write)")
        if r.skipped_rows:
            notes.append(f"{name}: {r.skipped_rows} malformed row(s) skipped")
    rows = lambda n: loaded[n].rows if loaded[n].present else None
    sm, rm = read_json(d / "sender.meta.json"), read_json(d / "receiver.meta.json")
    scen = read_json(d / "scenario.json")
    if not any((sm, rm, scen)) and not any(l.present for l in loaded.values()):
        raise FileNotFoundError(f"{d} does not look like a run directory")
    m = compute_metrics(rows("sent.csv"), rows("received.csv"), rows("echoes.csv"), rows("events.csv"),
                        sm, rm, scen, session_id=session_id, bin_s=bin_s,
                        handoff_margin_ms=handoff_margin_ms, load_notes=notes)
    m["inputs"] = {n: {"present": l.present, "rows": len(l.rows), "skipped_rows": l.skipped_rows,
                       "truncated_tail": l.truncated_tail} for n, l in loaded.items()}
    if scen:
        m["scenario_id"] = m["scenario_id"] or scen.get("scenario_id")
        m["run_id"] = m["run_id"] or scen.get("run_id")
    if write:
        (d / "metrics.json").write_text(json.dumps(m, indent=2, sort_keys=True))
    return m


def summary_text(m: dict) -> str:
    L, g, r = m["loss"], m["interarrival_gaps_ms"], m["rtt_ms"]
    out = [f"scenario={m['scenario_id']} run={m['run_id']} status={m['run_status']}",
           f"loss: {L['lost']}/{L['sent_ok']} ({L['loss_percent']}%) basis={L['basis']} exact={L['exact']}"
           f" longest_burst={L['longest_burst_packets']}",
           f"gaps ms: mean={g['mean']} p95={g['p95']} max={g['max']}",
           f"rtt ms: " + ("n/a" if not r or not r['count'] else f"min={r['min']} mean={r['mean']} p95={r['p95']} max={r['max']}"),
           f"jitter(rfc3550) ms: {m['jitter']['rfc3550_final_ms']}   goodput bps: {m['throughput']['goodput_bps']}"]
    for h in m.get("handoffs") or []:
        out.append(f"handoff {h.get('event_id')}: burst={h.get('loss_burst')} interruption_ms={h.get('interruption_ms')} "
                   f"rtt before/after={h.get('rtt_median_before_ms')}/{h.get('rtt_median_after_ms')}")
    for w in m["warnings"]:
        out.append(f"WARNING: {w}")
    return "\n".join(out)
