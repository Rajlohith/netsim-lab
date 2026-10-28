"""Feature extraction: every feature at decision time t is computed from observations with timestamp <= t ONLY.

`extract_features` returns, next to the values, `last_observation_ns` (the newest timestamp it touched) and asserts it is
<= t. tests/test_ml_features.py additionally proves the property by recomputing every feature on a copy of the run that has
been physically truncated at t (RunData.truncated).

Missing measurements are NaN, never zero-filled here (the model pipeline imputes with training-set statistics and adds
missing-value indicators). `UNAVAILABLE_FEATURES` lists what the project does not measure at all.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .config import FeatureConfig
from .runs import RunData

NAN = float("nan")


class LeakageError(AssertionError):
    """A feature used an observation newer than the decision time."""


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    group: str            # rtt | loss | throughput | timeout | history
    unit: str
    window: str
    source: str
    definition: str


FEATURES: tuple = (
    FeatureSpec("rtt_last_ms", "rtt", "ms", "latest echo", "sent.csv + echoes.csv", "RTT of the most recently ARRIVED echo (arrival - send, sender clock)"),
    FeatureSpec("rtt_mean_short_ms", "rtt", "ms", "short", "sent.csv + echoes.csv", "mean RTT of echoes that arrived in (t-short, t]"),
    FeatureSpec("rtt_max_short_ms", "rtt", "ms", "short", "sent.csv + echoes.csv", "max RTT of echoes that arrived in (t-short, t]"),
    FeatureSpec("rtt_std_short_ms", "rtt", "ms", "short", "sent.csv + echoes.csv", "sample std-dev of RTT in the short window (needs >= 2 echoes)"),
    FeatureSpec("rtt_mean_long_ms", "rtt", "ms", "long", "sent.csv + echoes.csv", "mean RTT of echoes that arrived in (t-long, t]"),
    FeatureSpec("rtt_trend_ms", "rtt", "ms", "short vs long", "sent.csv + echoes.csv", "rtt_mean_short - rtt_mean_long (positive = RTT rising)"),
    FeatureSpec("rtt_slope_ms_per_s", "rtt", "ms/s", "short", "sent.csv + echoes.csv", "least-squares slope of RTT over echo arrival time in the short window (needs >= 3 echoes)"),
    FeatureSpec("jitter_ms", "rtt", "ms", "short", "sent.csv + echoes.csv", "mean |RTT(seq+1) - RTT(seq)| over seq-consecutive echoes in the short window (RTT-delta jitter, as in traffic/metrics.py)"),
    FeatureSpec("loss_rate_short", "loss", "fraction", "short", "sent.csv + echoes.csv", "share of probes SENT in (t-short-grace, t-grace] whose echo had not arrived by t"),
    FeatureSpec("loss_rate_long", "loss", "fraction", "long", "sent.csv + echoes.csv", "same over (t-long-grace, t-grace]"),
    FeatureSpec("time_since_last_echo_ms", "loss", "ms", "latest echo", "echoes.csv", "t - arrival time of the newest echo"),
    FeatureSpec("echo_overdue_ratio", "loss", "probe intervals", "latest echo", "sent.csv + echoes.csv", "time_since_last_echo / probe interval, where the interval is the median spacing of the (up to 200) most recent probes sent <= t. In steady state it stays below 1 (it only reflects the phase of the probe clock); >= 1 means an echo is overdue"),
    FeatureSpec("goodput_short_kbps", "throughput", "kbit/s", "short", "echoes.csv", "echoed payload bits / observed span, short window (probe load is constant: this is delivered load, not capacity)"),
    FeatureSpec("goodput_long_kbps", "throughput", "kbit/s", "long", "echoes.csv", "same over the long window"),
    FeatureSpec("retransmit_count_long", "timeout", "count", "long", "approach_events.csv", "approach 'retransmit' events (mobility-signalling retransmissions) in (t-long, t]"),
    FeatureSpec("timeout_failure_count_long", "timeout", "count", "long", "approach_events.csv", "approach 'failure' events (registration timeout/failure, connection failure) in (t-long, t]"),
    FeatureSpec("handoffs_started_recent", "history", "count", "history_window", "events.csv", "handoffs whose handoff_start is in (t-history_window, t]"),
    FeatureSpec("handoffs_started_total", "history", "count", "run so far", "events.csv", "handoffs started at or before t"),
    FeatureSpec("time_since_last_handoff_start_s", "history", "s", "run so far", "events.csv", "t - start of the latest handoff that started <= t (NaN if none yet)"),
    FeatureSpec("time_since_last_handoff_end_s", "history", "s", "run so far", "events.csv", "t - end of the latest handoff that ENDED <= t (NaN if none yet)"),
    FeatureSpec("last_handoff_duration_ms", "history", "ms", "run so far", "events.csv", "end - start of the latest handoff that ended <= t (NaN if none yet)"),
    FeatureSpec("handoff_in_progress", "history", "0/1", "instant", "events.csv", "1 if a handoff started <= t has not ended by t"),
)
FEATURE_NAMES = tuple(f.name for f in FEATURES)
GROUPS = {g: tuple(f.name for f in FEATURES if f.group == g) for g in ("rtt", "loss", "throughput", "timeout", "history")}
MEASUREMENT_FEATURES = tuple(f.name for f in FEATURES if f.group != "history")
HISTORY_FEATURES = GROUPS["history"]
FEATURE_SETS = {"all": FEATURE_NAMES, "measurements_only": MEASUREMENT_FEATURES}

# Which approaches record the events behind the 'timeout' features in approach_events.csv.
#   retransmit: only the Mobile-IP family logs registration retransmissions ('reg_retransmit' -> 'retransmit').
#               TcpLite counts TCP retransmissions internally but does NOT write them to approach_events.csv.
#   failure   : every approach can log 'failure' events.
EVENT_CAPABILITY = {
    "retransmit_count_long": {"mip-basic", "mip-mbb", "ro-model"},
    "timeout_failure_count_long": {"mip-basic", "mip-mbb", "ro-model", "tcp-reconnect", "quic-migration"},
}

UNAVAILABLE_FEATURES = (
    ("signal_strength_dbm", "no radio model: the emulator has fixed link delays and an up/down state. Simulated network measurements are NOT physical radio signal strength."),
    ("snr_db", "no radio model (see signal_strength_dbm)"),
    ("link_quality_indicator", "the emulator exposes no link-layer quality indicator"),
    ("position_or_speed", "the simulated mobile node has no position or velocity; movement is a scheduled interface switch"),
    ("queue_length_or_buffer_occupancy", "simnet has no queues (documented in docs/MOBILITY_COMPARISON.md)"),
    ("available_bandwidth_estimate", "probes are a constant low-rate load; they cannot estimate capacity"),
    ("tcp_retransmissions_for_tcp_approach", "TcpLite keeps a retransmission counter but the adapter does not log it, so it is not observable from the run files"),
    ("owd_ms", "one-way delay needs receiver-side timestamps in the same clock domain; available only in same-host runs and deliberately not used (the predictor runs at the sender)"),
    ("quic_path_validation_signal", "aioquic is not installed here; the QUIC adapter was never run (PROJECT_STATUS K1)"),
)


def feature_availability(approach: str | None, approach_events_present: bool) -> dict:
    """{feature: (available, reason)} for a run of this approach. 'Unavailable' => NaN in every row of the run."""
    out = {}
    for f in FEATURES:
        ok, why = True, ""
        if f.name in EVENT_CAPABILITY:
            if not approach_events_present:
                ok, why = False, "approach_events.csv not present in this run"
            elif approach not in EVENT_CAPABILITY[f.name]:
                ok, why = False, f"approach {approach!r} does not log these events"
        out[f.name] = (ok, why)
    return out


@dataclass
class FeatureResult:
    values: dict
    last_observation_ns: int | None


def _mean(a):
    return float(np.mean(a)) if len(a) else NAN


def extract_features(run: RunData, t_ns: int, cfg: FeatureConfig) -> FeatureResult:
    t = int(t_ns)
    ws, wl, grace = int(cfg.short_window_s * 1e9), int(cfg.long_window_s * 1e9), int(cfg.loss_grace_ms * 1e6)
    newest = [None]

    def touch(x):
        if x is not None and (newest[0] is None or x > newest[0]):
            newest[0] = int(x)

    er = run.echo_recv_ns
    hi = int(np.searchsorted(er, t, side="right"))             # echoes that have ARRIVED by t
    lo_s = int(np.searchsorted(er, t - ws, side="right"))
    lo_l = int(np.searchsorted(er, t - wl, side="right"))
    if hi:
        touch(er[hi - 1])
    rtt_s, rtt_l = run.echo_rtt_ms[lo_s:hi], run.echo_rtt_ms[lo_l:hi]
    v = {n: NAN for n in FEATURE_NAMES}

    if hi:
        v["rtt_last_ms"] = float(run.echo_rtt_ms[hi - 1])
        v["time_since_last_echo_ms"] = (t - int(er[hi - 1])) / 1e6
    if len(rtt_s):
        v["rtt_mean_short_ms"], v["rtt_max_short_ms"] = float(rtt_s.mean()), float(rtt_s.max())
    if len(rtt_s) >= 2:
        v["rtt_std_short_ms"] = float(rtt_s.std(ddof=1))
    if len(rtt_l):
        v["rtt_mean_long_ms"] = float(rtt_l.mean())
    if len(rtt_s) and len(rtt_l):
        v["rtt_trend_ms"] = v["rtt_mean_short_ms"] - v["rtt_mean_long_ms"]
    if len(rtt_s) >= 3:
        x = (er[lo_s:hi] - t) / 1e9
        xc = x - x.mean()
        den = float((xc ** 2).sum())
        if den > 0:
            v["rtt_slope_ms_per_s"] = float((xc * (rtt_s - rtt_s.mean())).sum() / den)
    if len(rtt_s) >= 2:
        order = np.argsort(run.echo_seq[lo_s:hi], kind="stable")
        sq, rr = run.echo_seq[lo_s:hi][order], rtt_s[order]
        pairs = np.diff(sq) == 1
        if pairs.any():
            v["jitter_ms"] = float(np.abs(np.diff(rr))[pairs].mean())

    # overdue ratio: time since the last echo in units of the probe interval (interval known to the sender from its own schedule)
    k_sent = int(np.searchsorted(run.sent_ns, t, side="right"))
    if hi and k_sent >= 3:
        recent = run.sent_ns[max(0, k_sent - 201):k_sent]
        iv = float(np.median(np.diff(recent)))
        if iv > 0:
            touch(recent[-1])
            v["echo_overdue_ratio"] = (t - int(er[hi - 1])) / iv

    # loss: probes older than `grace` that have no echo by t
    for name, w in (("loss_rate_short", ws), ("loss_rate_long", wl)):
        a = int(np.searchsorted(run.sent_ns, t - w - grace, side="right"))
        b = int(np.searchsorted(run.sent_ns, t - grace, side="right"))
        if b > a:
            touch(run.sent_ns[b - 1])
            got = int((run.first_echo_by_sent[a:b] <= t).sum())
            v[name] = 1.0 - got / (b - a)

    # throughput (observed span shrinks at the start of a run instead of reporting a misleading low value)
    if hi:
        first = int(er[0])
        for name, lo, w in (("goodput_short_kbps", lo_s, ws), ("goodput_long_kbps", lo_l, wl)):
            span = min(w, t - first)
            if span > 0:
                v[name] = float(run.echo_bytes[lo:hi].sum()) * 8 / (span / 1e9) / 1000.0

    # approach events (retransmit / failure)
    av = feature_availability(run.approach, run.approach_events_present)
    k_hi = int(np.searchsorted(run.ev_ns, t, side="right"))
    k_lo = int(np.searchsorted(run.ev_ns, t - wl, side="right"))
    kinds = run.ev_kind[k_lo:k_hi]
    if av["retransmit_count_long"][0]:
        v["retransmit_count_long"] = float(sum(1 for k in kinds if k == "retransmit"))
    if av["timeout_failure_count_long"][0]:
        v["timeout_failure_count_long"] = float(sum(1 for k in kinds if k == "failure"))
    if k_hi:
        touch(run.ev_ns[k_hi - 1])

    # recent handoff history (events.csv): only handoffs that STARTED <= t; an end counts only if it is <= t as well
    started = [h for h in run.handoffs if h.start_ns <= t]
    v["handoffs_started_total"] = float(len(started))
    v["handoffs_started_recent"] = float(sum(1 for h in started if h.start_ns > t - int(cfg.history_window_s * 1e9)))
    v["handoff_in_progress"] = 0.0
    if started:
        touch(max(h.start_ns for h in started))
        v["time_since_last_handoff_start_s"] = (t - max(h.start_ns for h in started)) / 1e9
        ended = [h for h in started if h.end_ns is not None and h.end_ns <= t]
        v["handoff_in_progress"] = float(len(ended) < len(started))
        if ended:
            last = max(ended, key=lambda h: h.end_ns)
            touch(last.end_ns)
            v["time_since_last_handoff_end_s"] = (t - last.end_ns) / 1e9
            v["last_handoff_duration_ms"] = (last.end_ns - last.start_ns) / 1e6

    if newest[0] is not None and newest[0] > t:
        raise LeakageError(f"feature used an observation at {newest[0]} ns, later than the decision time {t} ns")
    # an unavailable feature must be NaN in every row (never a fabricated 0)
    for name, (ok, _why) in av.items():
        if not ok:
            v[name] = NAN
    return FeatureResult(v, newest[0])


def schema_dict() -> dict:
    """Machine-readable feature schema (written into every dataset / model bundle)."""
    return {
        "schema_version": 1,
        "features": [{"name": f.name, "group": f.group, "unit": f.unit, "window": f.window, "source": f.source,
                      "definition": f.definition, "missing_value": "NaN (imputed with the training median + indicator in the model pipeline)"}
                     for f in FEATURES],
        "feature_sets": {k: list(v) for k, v in FEATURE_SETS.items()},
        "approach_event_capability": {k: sorted(v) for k, v in EVENT_CAPABILITY.items()},
        "unavailable_features": [{"name": n, "reason": r} for n, r in UNAVAILABLE_FEATURES],
        "notes": ["All measurements are SIMULATED network measurements from the Stage 4/5 emulator (virtual time, fixed link delays).",
                  "No feature is, or approximates, physical radio signal strength."],
    }


def is_nan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)
