"""Mobile-IP metrics derived ONLY from mip_events*.csv (+ optional Stage 3 probe metrics).
Unavailable values are null with a reason; nothing is interpolated.

registration_delay  MN request sent -> matching authenticated reply received (MN clock only).
time_to_registered  attach (care-of address acquired) -> first accepted reply; includes retransmissions/resync.
handoff_latency     link_change (movement) -> first accepted reply = detection + registration.
tunnel overhead     bytes added by IP-in-IP: simulation counts real encapsulated bytes; for the live kernel
                    tunnel only the constant 20-byte RFC 2003 header is known (analytic, NOT measured).
interruption        taken from the Stage 3 probe metrics (loss burst / arrival gap around the handoff).
"""
from __future__ import annotations

import re

from ..traffic.metrics import stats
from .ipip import OVERHEAD

NS_MS = 1e6
_FAIL_KINDS = ("auth_failure", "replay_rejected", "reply_auth_invalid", "request_malformed", "reg_rejected",
               "reg_timeout", "reg_failed", "dataplane_error", "binding_expired")


def _ms(a, b):
    return None if a is None or b is None else round((b - a) / NS_MS, 6)


def compute_mip_metrics(events, summary=None, traffic_metrics=None, notes=None) -> dict:
    warnings = list(notes or [])
    if not events:
        return {"schema_version": 1, "available": False, "warnings": warnings + ["no Mobile IP events"]}
    ev = events
    mn = [e for e in ev if e["node"] == "mn"]
    ha = [e for e in ev if e["node"] == "ha"]

    # ---- registrations (request -> reply on the MN clock) ----------------------------------------
    sent = {}
    regs, retransmits = [], 0
    for e in mn:
        if e["kind"] == "reg_request_sent" and e["identification"] is not None:
            sent[e["identification"]] = e
        elif e["kind"] == "reg_reply_received" and e["identification"] in sent:
            s = sent[e["identification"]]
            regs.append({"identification": e["identification"], "care_of_address": e["care_of_address"],
                         "requested_lifetime_s": s["lifetime_s"], "granted_lifetime_s": e["lifetime_s"],
                         "code": e["code"], "delay_ms": _ms(s["mono_ns"], e["mono_ns"]),
                         "request_mono_ns": s["mono_ns"], "reply_mono_ns": e["mono_ns"]})
        elif e["kind"] == "reg_retransmit":
            retransmits += 1
    ok_delays = [r["delay_ms"] for r in regs if r["code"] in (0, 1) and r["requested_lifetime_s"] not in (0,)]

    # ---- episodes: link_change -> attach -> registered ---------------------------------------------
    episodes, cur, last_link = [], None, None
    for e in mn:
        k = e["kind"]
        if k == "link_change":
            last_link = e
        elif k == "attach":
            if cur:
                episodes.append(cur)
            cur = {"care_of_address": e["care_of_address"], "link_change_mono_ns": last_link["mono_ns"] if last_link else None,
                   "attach_mono_ns": e["mono_ns"], "registered_mono_ns": None, "outcome": "incomplete",
                   "requests": 0, "retransmits": 0, "rejected_133": 0}
            last_link = None
        elif cur is not None:
            if k == "reg_request_sent":
                cur["requests"] += 1
            elif k == "reg_retransmit":
                cur["retransmits"] += 1
            elif k == "reg_rejected" and e["code"] == 133:
                cur["rejected_133"] += 1
            elif k == "reg_reply_received" and e["code"] in (0, 1) and cur["registered_mono_ns"] is None \
                    and (e["lifetime_s"] or 0) > 0:
                cur["registered_mono_ns"], cur["outcome"] = e["mono_ns"], "registered"
            elif k in ("reg_failed", "reg_timeout") and cur["outcome"] == "incomplete":
                cur["outcome"] = "failed"
    if cur:
        episodes.append(cur)
    for c in episodes:
        c["detection_ms"] = _ms(c["link_change_mono_ns"], c["attach_mono_ns"])
        c["time_to_registered_ms"] = _ms(c["attach_mono_ns"], c["registered_mono_ns"])
        c["handoff_latency_ms"] = _ms(c["link_change_mono_ns"], c["registered_mono_ns"])

    # ---- HA binding timeline: which care-of address is the MN reachable at, and when -----------------
    timeline = []
    for e in ha:
        if e["kind"] in ("binding_created", "binding_updated"):
            timeline.append({"mono_ns": e["mono_ns"], "event": e["kind"], "reachable_via": e["care_of_address"],
                             "lifetime_s": e["lifetime_s"]})
        elif e["kind"] in ("binding_removed", "binding_expired"):
            timeline.append({"mono_ns": e["mono_ns"], "event": e["kind"], "reachable_via": None, "detail": e["detail"]})

    # ---- tunnel --------------------------------------------------------------------------------
    tunnel = {"measured": False, "basis": None, "packets": None, "overhead_bytes_per_packet": OVERHEAD,
              "overhead_percent_of_inner": None, "first_encap_after_binding_ms": []}
    st = next((e for e in reversed(ha) if e["kind"] == "tunnel_stats"), None)
    m = re.search(r"packets=(\d+) inner_bytes=(\d+) outer_bytes=(\d+)", st["detail"]) if st else None
    if m and int(m.group(1)) > 0:
        pk, inner, outer = (int(x) for x in m.groups())
        tunnel.update(measured=True, basis=(summary or {}).get("tunnel_overhead_basis", "counted bytes"),
                      packets=pk, inner_bytes=inner, outer_bytes=outer, overhead_bytes_per_packet=(outer - inner) / pk,
                      overhead_percent_of_inner=round(100.0 * (outer - inner) / inner, 6))
    else:
        payload = (summary or {}).get("probe_payload_bytes")
        tunnel["basis"] = "analytic: RFC 2003 outer header; kernel tunnel bytes are not observed by this tool"
        if payload:
            tunnel["overhead_percent_of_inner"] = round(100.0 * OVERHEAD / (20 + 8 + payload), 6)
        warnings.append("tunnel overhead is analytic (no tunnel_stats event)")
    for en in (e for e in ha if e["kind"] == "tunnel_encap"):
        b = [t for t in timeline if t["event"] in ("binding_created", "binding_updated")
             and t["reachable_via"] == en["care_of_address"] and t["mono_ns"] <= en["mono_ns"]]
        if b:
            tunnel["first_encap_after_binding_ms"].append({"care_of_address": en["care_of_address"],
                                                           "ms": _ms(b[-1]["mono_ns"], en["mono_ns"])})

    # ---- failures ------------------------------------------------------------------------------
    failures = {k: sum(1 for e in ev if e["kind"] == k) for k in _FAIL_KINDS}
    failures["tunnel_drops_first_seen"] = [e["detail"] for e in ev if e["kind"] == "tunnel_drop"]
    failures["packet_drops_by_reason"] = (summary or {}).get("drops")

    # ---- connectivity interruption (from probes) -----------------------------------------------
    conn = None
    if traffic_metrics:
        hs = traffic_metrics.get("handoffs") or []
        conn = {"source": "Stage 3 probe metrics", "loss": {k: traffic_metrics["loss"].get(k) for k in
                                                             ("lost", "sent_ok", "loss_percent", "exact", "longest_burst_packets")},
                "handoffs": [{"event_id": h.get("event_id"), "detail": h.get("detail"),
                              "loss_burst_packets": (h.get("loss_burst") or {}).get("packets"),
                              "interruption_ms": h.get("interruption_ms"), "recovered": h.get("recovered"),
                              "rtt_median_before_ms": h.get("rtt_median_before_ms"),
                              "rtt_median_after_ms": h.get("rtt_median_after_ms")} for h in hs],
                "unavailable_reason": traffic_metrics.get("handoffs_unavailable_reason")}
        moved = [c for c in episodes if c["link_change_mono_ns"] is not None]
        for h, c in zip(conn["handoffs"], moved):
            h["handoff_latency_ms"] = c["handoff_latency_ms"]
            h["time_to_registered_ms"] = c["time_to_registered_ms"]
    else:
        warnings.append("no probe data: connectivity interruption not available")

    return {"schema_version": 1, "available": True, "mode": (summary or {}).get("mode"),
            "registrations": regs, "registration_delay_ms": stats(ok_delays), "retransmissions": retransmits,
            "episodes": episodes, "binding_timeline": timeline, "tunnel": tunnel, "failures": failures,
            "connectivity": conn, "warnings": warnings}
