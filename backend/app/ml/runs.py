"""Load one experiment run directory into numpy arrays, without repairing or inventing anything.

Files used (all written by Stages 3-5): sent.csv, echoes.csv, events.csv (+ optional approach_events.csv, experiment.json,
comparison_metrics.json). `received.csv` is deliberately NOT used: it is receiver-side data and the predictor runs where
the probe SENDER runs (RTT is measured on a single clock, the sender's).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from ..compare import events as ev
from ..traffic.loader import read_csv, read_json

NS = 1e9


@dataclass(frozen=True)
class Handoff:
    event_id: str
    start_ns: int
    end_ns: int | None
    detail: str = ""


@dataclass
class RunData:
    experiment_id: str
    scenario_id: str
    run_id: str
    approach: str | None
    data_origin: str
    sent_seq: np.ndarray                 # probes with send_errno == 0, sorted by seq
    sent_ns: np.ndarray                  # their send times (monotonic ns of the sender)
    echo_recv_ns: np.ndarray             # FIRST echo of each seq, sorted by arrival time
    echo_seq: np.ndarray
    echo_rtt_ms: np.ndarray              # arrival - send, both on the sender's clock
    echo_bytes: np.ndarray
    handoffs: list                       # list[Handoff], sorted by start
    ev_ns: np.ndarray                    # approach_events.csv timestamps (sorted)
    ev_kind: np.ndarray                  # ... and kinds
    approach_events_present: bool
    scenario_start_ns: int | None
    scenario_end_ns: int | None
    warnings: list = field(default_factory=list)
    run_dir: Path | None = None
    tags: dict = field(default_factory=dict)       # optional experiment-generator labels (ml_family.json), e.g. {'dwell_model': 'uniform'}

    # ---- derived ---------------------------------------------------------------------------------------------------
    def __post_init__(self):
        n = len(self.sent_ns)
        self.first_echo_by_sent = np.full(n, np.iinfo(np.int64).max, dtype=np.int64)   # arrival of the first echo, per probe
        if n and len(self.echo_seq):
            pos = np.searchsorted(self.sent_seq, self.echo_seq)
            pos = np.clip(pos, 0, n - 1)
            hit = self.sent_seq[pos] == self.echo_seq
            self.first_echo_by_sent[pos[hit]] = self.echo_recv_ns[hit]

    @property
    def nominal_interval_ns(self) -> float | None:
        """Median spacing of probe send times (the configured probe interval, as actually executed)."""
        if len(self.sent_ns) < 2:
            return None
        return float(np.median(np.diff(self.sent_ns)))

    @property
    def first_probe_ns(self) -> int | None:
        return int(self.sent_ns[0]) if len(self.sent_ns) else None

    @property
    def observed_end_ns(self) -> int | None:
        """Last instant for which the run has data: the scenario_end event if present, else the last probe/echo."""
        if self.scenario_end_ns is not None:
            return int(self.scenario_end_ns)
        c = [int(a[-1]) for a in (self.sent_ns, self.echo_recv_ns) if len(a)]
        return max(c) if c else None

    def truncated(self, t_ns: int) -> "RunData":
        """A copy containing ONLY what could have been observed at time t_ns (used to test and enforce no-leakage)."""
        k_s = int(np.searchsorted(self.sent_ns, t_ns, side="right"))
        k_e = int(np.searchsorted(self.echo_recv_ns, t_ns, side="right"))
        k_v = int(np.searchsorted(self.ev_ns, t_ns, side="right"))
        hs = [replace(h, end_ns=h.end_ns if (h.end_ns is not None and h.end_ns <= t_ns) else None)
              for h in self.handoffs if h.start_ns <= t_ns]
        return RunData(self.experiment_id, self.scenario_id, self.run_id, self.approach, self.data_origin,
                       self.sent_seq[:k_s], self.sent_ns[:k_s], self.echo_recv_ns[:k_e], self.echo_seq[:k_e],
                       self.echo_rtt_ms[:k_e], self.echo_bytes[:k_e], hs, self.ev_ns[:k_v], self.ev_kind[:k_v],
                       self.approach_events_present,
                       self.scenario_start_ns if (self.scenario_start_ns is not None and self.scenario_start_ns <= t_ns) else None,
                       None, list(self.warnings), self.run_dir, dict(self.tags))

    @classmethod
    def from_arrays(cls, *, experiment_id="exp", scenario_id="scn", run_id="r1", approach="mip-basic", data_origin="synthetic-test",
                    sent_ns, echo_seq_recv_ns: dict | None = None, rtt_ms: float = 30.0, handoffs=(), events=(),
                    approach_events_present=True, scenario_start_ns=None, scenario_end_ns=None, size=64) -> "RunData":
        """Build a RunData from plain numbers. FOR UNIT TESTS ONLY (synthetic input, never used for reported results).
        echo_seq_recv_ns: {seq: recv_ns}; default = every probe echoed after `rtt_ms`."""
        sent_ns = np.asarray(sent_ns, dtype=np.int64)
        seq = np.arange(len(sent_ns), dtype=np.int64)
        if echo_seq_recv_ns is None:
            echo_seq_recv_ns = {int(s): int(t + rtt_ms * 1e6) for s, t in zip(seq, sent_ns)}
        items = sorted(echo_seq_recv_ns.items(), key=lambda kv: kv[1])
        e_seq = np.array([k for k, _ in items], dtype=np.int64)
        e_recv = np.array([v for _, v in items], dtype=np.int64)
        e_rtt = (e_recv - sent_ns[e_seq]) / 1e6 if len(e_seq) else np.zeros(0)
        evs = sorted(events)
        hs = [h if isinstance(h, Handoff) else Handoff(*h) for h in handoffs]
        hs.sort(key=lambda h: h.start_ns)
        return cls(experiment_id, scenario_id, run_id, approach, data_origin, seq, sent_ns, e_recv, e_seq, e_rtt,
                   np.full(len(e_seq), size, dtype=np.int64), hs, np.array([t for t, _ in evs], dtype=np.int64),
                   np.array([k for _, k in evs], dtype=object), approach_events_present, scenario_start_ns, scenario_end_ns)


class RunLoadError(Exception):
    pass


def load_run(run_dir) -> RunData:
    d = Path(run_dir)
    exp = read_json(d / "experiment.json")
    if exp is not None and exp.get("status") not in (None, "ok"):
        raise RunLoadError(f"{d}: experiment status is {exp.get('status')!r} ({exp.get('reason')})")
    for name in ("sent.csv", "echoes.csv", "events.csv"):
        if not (d / name).exists():
            raise RunLoadError(f"{d}: missing {name}")
    warnings = []
    sent = read_csv(d / "sent.csv", "sent.csv")
    echoes = read_csv(d / "echoes.csv", "echoes.csv")
    events = read_csv(d / "events.csv", "events.csv")
    for nm, r in (("sent.csv", sent), ("echoes.csv", echoes), ("events.csv", events)):
        if r.skipped_rows or r.truncated_tail:
            warnings.append(f"{nm}: {r.skipped_rows} malformed rows skipped, truncated_tail={r.truncated_tail}")
    ok = sorted((r for r in sent.rows if r["send_errno"] == 0), key=lambda r: r["seq"])
    if not ok:
        raise RunLoadError(f"{d}: no successfully sent probes")
    sent_seq = np.array([r["seq"] for r in ok], dtype=np.int64)
    sent_ns = np.array([r["send_mono_ns"] for r in ok], dtype=np.int64)
    if np.any(np.diff(sent_ns) < 0):
        raise RunLoadError(f"{d}: send times are not monotonic in seq order")
    by_seq = dict(zip(sent_seq.tolist(), sent_ns.tolist()))
    seen, e_seq, e_recv, e_rtt, e_bytes = set(), [], [], [], []
    orphan = neg = 0
    for r in sorted(echoes.rows, key=lambda r: (r["recv_mono_ns"], r["seq"])):
        if r["seq"] in seen:
            continue
        s = by_seq.get(r["seq"])
        if s is None:
            orphan += 1
            continue
        if r["recv_mono_ns"] < s:
            neg += 1
            continue
        seen.add(r["seq"])
        e_seq.append(r["seq"]); e_recv.append(r["recv_mono_ns"]); e_rtt.append((r["recv_mono_ns"] - s) / 1e6)
        e_bytes.append(r["size_bytes"])
    if orphan:
        warnings.append(f"{orphan} echoes without a sent row ignored")
    if neg:
        warnings.append(f"{neg} echoes with negative RTT ignored")
    starts, ends, s_start, s_end = {}, {}, None, None
    for r in events.rows:
        if r["kind"] == "handoff_start":
            starts[r["event_id"]] = r
        elif r["kind"] == "handoff_end":
            ends[r["event_id"]] = r
        elif r["kind"] == "scenario_start":
            s_start = r["mono_ns"]
        elif r["kind"] == "scenario_end":
            s_end = r["mono_ns"]
    hs = sorted((Handoff(k, v["mono_ns"], ends[k]["mono_ns"] if k in ends else None, v["detail"]) for k, v in starts.items()),
                key=lambda h: h.start_ns)
    ae = ev.read_events(d / "approach_events.csv")
    rows = sorted(ae or [], key=lambda r: r["mono_ns"])
    spec = (exp or {}).get("spec") or {}
    meta = read_json(d / "comparison_metrics.json") or {}
    scenario_id = spec.get("scenario_id") or (read_json(d / "scenario.json") or {}).get("scenario_id") or d.parent.name
    run_id = spec.get("run_id") or d.name
    return RunData(f"{scenario_id}/{run_id}", scenario_id, run_id, spec.get("approach") or meta.get("approach"),
                   (meta.get("provenance") or {}).get("data_origin", "unknown"),
                   sent_seq, sent_ns, np.array(e_recv, dtype=np.int64), np.array(e_seq, dtype=np.int64),
                   np.array(e_rtt, dtype=float), np.array(e_bytes, dtype=np.int64), hs,
                   np.array([r["mono_ns"] for r in rows], dtype=np.int64), np.array([r["kind"] for r in rows], dtype=object),
                   ae is not None, s_start, s_end, warnings, d,
                   (read_json(d / "ml_family.json") or {}))


def discover_runs(root) -> list:
    """All run directories under root (a dir with sent.csv + echoes.csv + events.csv), sorted for reproducibility."""
    root = Path(root)
    found = sorted({p.parent for p in root.rglob("sent.csv")})
    return [p for p in found if (p / "echoes.csv").exists() and (p / "events.csv").exists()]
