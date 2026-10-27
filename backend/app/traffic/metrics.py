"""Metrics computed ONLY from raw observations. Anything that cannot be measured is null (None),
with a reason in `warnings` / `*_unavailable_reason`; nothing is interpolated or assumed.

Definitions (also in docs/TRAFFIC_MEASUREMENT.md):
 loss          sent_ok seqs never received / sent_ok. Needs the sender log; otherwise only the lower
               bound from receiver-side sequence gaps is given.
 gaps          differences between consecutive first-arrival times at the receiver (monotonic).
 rtt           echo_recv_mono - send_mono, both from the SENDER's monotonic clock (single clock).
 owd           recv_mono - send_mono across two processes: only valid when both ran on the same
               kernel boot (same boot_id); otherwise null.
 jitter        RFC 3550 interarrival jitter on transit-time differences (clock offset cancels).
 throughput    bytes / time span; for fixed-rate probes this is offered/delivered load, not capacity.
"""
from __future__ import annotations

import math
import statistics

from . import clock
from .schema import SCHEMA_VERSION

NS_MS = 1e6


def _r(x, nd=6):
    return None if x is None else round(x, nd)


def _pct(sorted_vals, p):
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def stats(vals) -> dict:
    n = len(vals)
    if n == 0:
        return {"count": 0, "min": None, "mean": None, "p50": None, "p95": None, "p99": None,
                "max": None, "stdev": None}
    s = sorted(vals)
    return {"count": n, "min": _r(s[0]), "mean": _r(sum(s) / n), "p50": _r(_pct(s, 50)),
            "p95": _r(_pct(s, 95)), "p99": _r(_pct(s, 99)), "max": _r(s[-1]),
            "stdev": _r(statistics.stdev(s)) if n > 1 else None}


def _bursts(lost_seqs, sent_by_seq):
    out, run = [], []
    for s in sorted(lost_seqs):
        if run and s == run[-1] + 1:
            run.append(s)
        else:
            if run:
                out.append(run)
            run = [s]
    if run:
        out.append(run)
    res = []
    for r in out:
        res.append({"first_seq": r[0], "last_seq": r[-1], "count": len(r),
                    "first_send_mono_ns": sent_by_seq[r[0]]["send_mono_ns"],
                    "last_send_mono_ns": sent_by_seq[r[-1]]["send_mono_ns"]})
    return res


def _span_rate(total_bytes, t_first, t_last):
    if t_last is None or t_first is None or t_last <= t_first:
        return None
    return total_bytes * 8 / ((t_last - t_first) / 1e9)


def compute_metrics(sent, received, echoes, events, sender_meta, receiver_meta, scenario_meta=None,
                    *, session_id=None, bin_s=1.0, handoff_margin_ms=250.0, load_notes=None) -> dict:
    """sent/received/echoes/events: lists of row dicts (as parsed by loader.read_csv) or None if the
    file does not exist."""
    warnings = list(load_notes or [])
    sid = session_id if session_id is not None else (sender_meta or {}).get("session_id")

    # ---- run status -------------------------------------------------------------------------
    def st(m):
        return None if m is None else m.get("status")
    statuses = {"sender": st(sender_meta), "receiver": st(receiver_meta)}
    if all(v == "completed" for v in statuses.values()):
        run_status = "complete"
    elif all(v in ("completed", "interrupted") for v in statuses.values()):
        run_status = "interrupted"
    else:
        run_status = "incomplete"
        for role, v in statuses.items():
            if v is None:
                warnings.append(f"{role} metadata missing: that side's data may be absent or unfinalised")
            elif v in ("running", "failed"):
                warnings.append(f"{role} status is '{v}': run did not finish cleanly, data may be truncated")

    # ---- received (filter session, first arrivals) --------------------------------------------
    rx_all = received or []
    sessions = sorted({r["session_id"] for r in rx_all})
    if sid is None and len(sessions) == 1:
        sid = sessions[0]
    elif sid is None and len(sessions) > 1:
        warnings.append(f"multiple sessions in received.csv {sessions}; pass session_id")
    rx = [r for r in rx_all if sid is None or r["session_id"] == sid]
    if received is not None and len(rx) != len(rx_all):
        warnings.append(f"{len(rx_all) - len(rx)} received rows from other sessions ignored")
    first = {}
    dups = 0
    reordered = 0
    max_seq = -1
    arrivals = []                      # first arrivals, in file (= arrival) order
    for r in rx:
        if r["seq"] in first:
            dups += 1
            continue
        first[r["seq"]] = r
        if r["seq"] < max_seq:
            reordered += 1
        max_seq = max(max_seq, r["seq"])
        arrivals.append(r)

    sent_rows = sent or []
    sent_by_seq = {r["seq"]: r for r in sent_rows}
    sent_ok = {s for s, r in sent_by_seq.items() if r["send_errno"] == 0}

    # ---- loss ---------------------------------------------------------------------------------
    loss = {"basis": None, "exact": False, "sent_ok": None, "delivered": None, "lost": None,
            "loss_percent": None, "lost_seqs": None, "bursts": [], "longest_burst_packets": None,
            "send_errors": sum(1 for r in sent_rows if r["send_errno"] != 0),
            "received_not_in_sent_log": None}
    if sent is not None and sent_ok:
        delivered = set(first) & sent_ok
        lost = sorted(sent_ok - delivered)
        unlogged = len(set(first) - set(sent_by_seq))
        bursts = _bursts(lost, sent_by_seq)
        counters = (sender_meta or {}).get("counters", {})
        sender_final = st(sender_meta) in ("completed", "interrupted") and \
            counters.get("sent_rows") == len(sent_rows)
        receiver_final = st(receiver_meta) in ("completed", "interrupted")
        exact = bool(sender_final and receiver_final and unlogged == 0 and received is not None)
        if not exact:
            why = []
            if not sender_final:
                why.append("sender log not finalised/complete")
            if not receiver_final:
                why.append("receiver not finalised")
            if unlogged:
                why.append(f"{unlogged} received seqs missing from sender log")
            if received is None:
                why.append("received.csv missing")
            warnings.append("loss is not exact: " + "; ".join(why))
        loss.update(basis="sender_log", exact=exact, sent_ok=len(sent_ok), delivered=len(delivered),
                    lost=len(lost), loss_percent=_r(100.0 * len(lost) / len(sent_ok)),
                    lost_seqs=lost if len(lost) <= 10_000 else None,
                    bursts=[{k: v for k, v in b.items()} for b in bursts],
                    longest_burst_packets=max((b["count"] for b in bursts), default=0),
                    received_not_in_sent_log=unlogged)
    elif first:
        lo, hi = min(first), max(first)
        missing = (hi - lo + 1) - len(first)
        warnings.append("no usable sender log: loss is a LOWER BOUND from receiver-side seq gaps "
                        "(tail loss after the last received seq is unobservable)")
        loss.update(basis="receiver_gaps", exact=False, delivered=len(first), lost=missing,
                    loss_percent=_r(100.0 * missing / (hi - lo + 1)),
                    sent_ok=None)

    # ---- inter-arrival gaps -------------------------------------------------------------------
    by_time = sorted(arrivals, key=lambda r: r["recv_mono_ns"])
    gap_list = []
    for a, b in zip(by_time, by_time[1:]):
        gap_list.append((a["recv_mono_ns"], b["recv_mono_ns"], a["seq"], b["seq"]))
    gaps_ms = [(e - s) / NS_MS for s, e, *_ in gap_list]
    gap_info = stats(gaps_ms)
    if gap_list:
        s, e, qa, qb = max(gap_list, key=lambda g: g[1] - g[0])
        gap_info["max_gap_detail"] = {"after_seq": qa, "before_seq": qb, "start_mono_ns": s, "end_mono_ns": e}

    # ---- RTT ----------------------------------------------------------------------------------
    rtt = {}                       # seq -> rtt ms
    echo_without_sent = neg = 0
    for e in (echoes or []):
        if e["seq"] in rtt:
            continue
        s = sent_by_seq.get(e["seq"])
        if s is None:
            echo_without_sent += 1
            continue
        v = e["recv_mono_ns"] - s["send_mono_ns"]
        if v < 0:
            neg += 1
            continue
        rtt[e["seq"]] = v / NS_MS
    if echo_without_sent:
        warnings.append(f"{echo_without_sent} echoes had no sender row; excluded from RTT")
    if neg:
        warnings.append(f"{neg} echoes had negative RTT (impossible); excluded")
    rtt_stats = stats(list(rtt.values())) if (echoes is not None and sent is not None) else None
    rtt_seqs = sorted(rtt)
    drtt = [abs(rtt[b] - rtt[a]) for a, b in zip(rtt_seqs, rtt_seqs[1:]) if b == a + 1]
    echo_info = None
    if receiver_meta and receiver_meta.get("params", {}).get("echo") is False:
        echo_info = "receiver ran with echo disabled: no RTT available"
    elif echoes is not None:
        delivered_no_echo = len(set(first) & sent_ok) - len(set(rtt) & set(first)) if sent else None
        echo_info = {"echoed": len(rtt), "delivered_without_echo": delivered_no_echo}

    # ---- one-way delay (same clock domain only) -------------------------------------------------
    sb = ((sender_meta or {}).get("clock") or {}).get("boot_id")
    rb = ((receiver_meta or {}).get("clock") or {}).get("boot_id")
    same_domain = bool(sb and rb and sb == rb)
    owd_stats, owd_reason = None, None
    if arrivals and same_domain:
        owd_stats = stats([(r["recv_mono_ns"] - r["hdr_send_mono_ns"]) / NS_MS for r in arrivals])
    else:
        owd_reason = ("sender/receiver metadata missing" if not (sb and rb) else
                      "sender and receiver monotonic clocks are in different domains (different boot_id)") \
            if arrivals else "no packets received"

    # ---- jitter (RFC 3550) ----------------------------------------------------------------------
    j, prev, js = 0.0, None, []
    for r in arrivals:
        transit = (r["recv_mono_ns"] - r["hdr_send_mono_ns"]) / NS_MS
        if prev is not None:
            j += (abs(transit - prev) - j) / 16.0
            js.append(j)
        prev = transit
    jitter = {"rfc3550_final_ms": _r(js[-1]) if js else None, "rfc3550_mean_ms": _r(sum(js) / len(js)) if js else None,
              "rfc3550_max_ms": _r(max(js)) if js else None,
              "rtt_delta_mean_ms": _r(sum(drtt) / len(drtt)) if drtt else None, "samples": len(js)}

    # ---- throughput -----------------------------------------------------------------------------
    tp = {"offered_bps": None, "goodput_bps": None, "sent_bytes": None, "received_bytes": None,
          "timeseries_bin_s": bin_s, "timeseries": []}
    if sent_ok:
        ok_rows = sorted((sent_by_seq[s] for s in sent_ok), key=lambda r: r["send_mono_ns"])
        tp["sent_bytes"] = sum(r["size_bytes"] for r in ok_rows)
        tp["offered_bps"] = _r(_span_rate(sum(r["size_bytes"] for r in ok_rows[:-1]),
                                          ok_rows[0]["send_mono_ns"], ok_rows[-1]["send_mono_ns"]), 3)
    if by_time:
        tp["received_bytes"] = sum(r["size_bytes"] for r in by_time)
        tp["goodput_bps"] = _r(_span_rate(sum(r["size_bytes"] for r in by_time[1:]),
                                          by_time[0]["recv_mono_ns"], by_time[-1]["recv_mono_ns"]), 3)
        t0 = by_time[0]["recv_mono_ns"]
        nbins = int((by_time[-1]["recv_mono_ns"] - t0) / (bin_s * 1e9)) + 1
        bins = [[0, 0] for _ in range(nbins)]
        for r in by_time:
            b = int((r["recv_mono_ns"] - t0) / (bin_s * 1e9))
            bins[b][0] += 1
            bins[b][1] += r["size_bytes"]
        # bins lie between the first and last arrival, so an empty bin is a measured "nothing arrived"
        tp["timeseries"] = [{"t_start_s": _r(i * bin_s), "packets": p, "bytes": by, "bps": _r(by * 8 / bin_s, 3)}
                            for i, (p, by) in enumerate(bins)]
        tp["timeseries_origin_mono_ns"] = t0

    # ---- sender schedule slip -------------------------------------------------------------------
    slip = stats([(r["send_mono_ns"] - r["scheduled_mono_ns"]) / NS_MS for r in sent_rows]) if sent else None

    # ---- handoffs -------------------------------------------------------------------------------
    handoffs, handoff_reason = None, None
    if events:
        ev_boot = ((scenario_meta or {}).get("clock") or {}).get("boot_id")
        if not (ev_boot and sb and rb and ev_boot == sb == rb):
            handoff_reason = "event/sender/receiver clock domains cannot be proven identical (boot_id)"
        else:
            handoffs = _handoffs(events, sent_by_seq, sent_ok, first, by_time, gap_list, rtt, handoff_margin_ms,
                                 loss["bursts"])
    elif events is not None:
        handoff_reason = "events.csv has no rows"

    return {
        "schema_version": SCHEMA_VERSION,
        "scenario_id": (sender_meta or receiver_meta or scenario_meta or {}).get("scenario_id"),
        "run_id": (sender_meta or receiver_meta or scenario_meta or {}).get("run_id"),
        "session_id": sid,
        "generated_wall_iso": clock.iso_utc(clock.wall_ns()),
        "run_status": run_status, "role_status": statuses,
        "loss": loss,
        "duplicates": dups, "reordered_packets": reordered,
        "interarrival_gaps_ms": gap_info,
        "rtt_ms": rtt_stats, "rtt_info": echo_info,
        "owd_ms": owd_stats, "owd_unavailable_reason": owd_reason,
        "jitter": jitter, "throughput": tp, "sender_schedule_slip_ms": slip,
        "handoffs": handoffs, "handoffs_unavailable_reason": handoff_reason,
        "warnings": warnings,
    }


def _median(vals):
    return None if not vals else _r(statistics.median(vals))


def _handoffs(events, sent_by_seq, sent_ok, first, by_time, gap_list, rtt, margin_ms, bursts):
    starts, ends = {}, {}
    for e in events:
        if e["kind"] == "handoff_start":
            starts[e["event_id"]] = e
        elif e["kind"] == "handoff_end":
            ends[e["event_id"]] = e
    margin = int(margin_ms * 1e6)
    out = []
    for eid, s in sorted(starts.items(), key=lambda kv: kv[1]["mono_ns"]):
        ts = s["mono_ns"]
        end = ends.get(eid)
        te = end["mono_ns"] if end else ts
        lo, hi = ts - margin, te + margin
        h = {"event_id": eid, "detail": s["detail"], "start_mono_ns": ts, "end_mono_ns": end["mono_ns"] if end else None,
             "end_missing": end is None, "action_duration_ms": _r((te - ts) / NS_MS) if end else None,
             "margin_ms": margin_ms}
        ov = [b for b in bursts if b["first_send_mono_ns"] <= hi and b["last_send_mono_ns"] >= lo]
        h["overlapping_loss_bursts"] = len(ov)
        if ov:
            b = max(ov, key=lambda x: x["count"])
            prev = max((q for q in first if q < b["first_seq"]), default=None)
            nxt = min((q for q in first if q > b["last_seq"]), default=None)
            h["loss_burst"] = {"first_seq": b["first_seq"], "last_seq": b["last_seq"], "packets": b["count"],
                               "send_span_ms": _r((b["last_send_mono_ns"] - b["first_send_mono_ns"]) / NS_MS)}
            h["recovered"] = nxt is not None
            h["interruption_ms"] = _r((first[nxt]["recv_mono_ns"] - first[prev]["recv_mono_ns"]) / NS_MS) \
                if (prev is not None and nxt is not None) else None
        else:
            h["loss_burst"], h["recovered"], h["interruption_ms"] = None, True, None
        in_win = [(e_ - s_) / NS_MS for s_, e_, *_ in gap_list if s_ <= hi and e_ >= lo]
        h["max_interarrival_gap_in_window_ms"] = _r(max(in_win)) if in_win else None
        before = [v for q, v in sorted(rtt.items()) if sent_by_seq[q]["send_mono_ns"] < lo][-10:]
        after = [v for q, v in sorted(rtt.items()) if sent_by_seq[q]["send_mono_ns"] > hi][:10]
        h["rtt_median_before_ms"], h["rtt_median_after_ms"] = _median(before), _median(after)
        h["rtt_samples_before"], h["rtt_samples_after"] = len(before), len(after)
        out.append(h)
    for eid in set(ends) - set(starts):
        out.append({"event_id": eid, "error": "handoff_end without handoff_start"})
    return out
