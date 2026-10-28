"""Labelling rules (the ONLY module that looks at the future). See LabelConfig for the definition.

    degradation episode  = handoff + a service gap (interval between consecutive echo arrivals >= gap threshold)
    onset                = handoff_start instant
    label(t) = 1  iff  an episode onset lies in (t, t + horizon]
    excluded(t)          'in_episode'          onset <= t <= episode end (degradation already under way: nothing left to predict)
                         'censored_horizon'    t + horizon is beyond the end of the observed run (label unknowable)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

import numpy as np

from .config import LabelConfig
from .runs import RunData


@dataclass(frozen=True)
class Episode:
    experiment_id: str
    event_id: str
    onset_ns: int            # handoff_start
    end_ns: int              # end of the last matching gap (= run end if never recovered)
    gap_ms: float            # longest matching echo gap
    gap_start_ns: int
    recovered: bool

    def to_dict(self):
        return asdict(self)


def gap_threshold_ns(run: RunData, cfg: LabelConfig) -> float:
    iv = run.nominal_interval_ns
    return max(cfg.min_gap_ms * 1e6, cfg.gap_interval_factor * (iv or 0.0))


def find_gaps(run: RunData, thr_ns: float) -> list:
    """(gap_start_ns, gap_end_ns, recovered) for every interval between consecutive echo ARRIVALS >= thr, plus a trailing
    gap if the last echo is at least thr before the end of the observed run."""
    er = run.echo_recv_ns
    gaps = []
    if len(er) >= 2:
        d = np.diff(er)
        for i in np.nonzero(d >= thr_ns)[0]:
            gaps.append((int(er[i]), int(er[i + 1]), True))
    end = run.observed_end_ns
    if len(er) and end is not None and end - er[-1] >= thr_ns:
        gaps.append((int(er[-1]), int(end), False))
    elif not len(er) and end is not None and run.first_probe_ns is not None and end - run.first_probe_ns >= thr_ns:
        gaps.append((int(run.first_probe_ns), int(end), False))
    return sorted(gaps)


def detect_episodes(run: RunData, cfg: LabelConfig):
    """Returns (episodes, handoff_outcomes, diagnostics).

    handoff_outcomes: one dict per handoff {event_id, onset_ns, outcome} with outcome in
      'degraded' | 'benign' (no qualifying gap) | 'merged' (its gap belongs to an earlier handoff's episode).
    diagnostics: {'gap_threshold_ms', 'unrelated_gaps': [...]} (gaps that overlap no handoff window; never labelled positive).
    """
    thr = gap_threshold_ns(run, cfg)
    gaps = find_gaps(run, thr)
    used, episodes, outcomes = set(), [], []
    margin = int(cfg.handoff_margin_ms * 1e6)
    for h in run.handoffs:
        h_end = h.end_ns if h.end_ns is not None else h.start_ns + int(cfg.open_handoff_window_s * 1e9)
        match = [i for i, (gs, ge, _r) in enumerate(gaps) if ge > h.start_ns and gs <= h_end + margin]
        fresh = [i for i in match if i not in used]
        if fresh:
            used.update(fresh)
            sel = [gaps[i] for i in fresh]
            longest = max(sel, key=lambda g: g[1] - g[0])
            episodes.append(Episode(run.experiment_id, h.event_id, h.start_ns, max(g[1] for g in sel),
                                    (longest[1] - longest[0]) / 1e6, longest[0], all(g[2] for g in sel)))
            outcomes.append({"event_id": h.event_id, "onset_ns": h.start_ns, "outcome": "degraded"})
        elif match:
            outcomes.append({"event_id": h.event_id, "onset_ns": h.start_ns, "outcome": "merged"})
        else:
            outcomes.append({"event_id": h.event_id, "onset_ns": h.start_ns, "outcome": "benign"})
    unrelated = [{"gap_start_ns": gaps[i][0], "gap_end_ns": gaps[i][1]} for i in range(len(gaps)) if i not in used
                 and not any(gaps[i][1] > h.start_ns and gaps[i][0] <= (h.end_ns or h.start_ns + int(cfg.open_handoff_window_s * 1e9)) + margin
                             for h in run.handoffs)]
    return episodes, outcomes, {"gap_threshold_ms": thr / 1e6, "unrelated_gaps": unrelated, "gaps_total": len(gaps)}


def label_decision_points(times_ns, episodes, observed_end_ns, cfg: LabelConfig) -> list:
    """For each decision time: dict(label, status, episode_event_id, time_to_onset_s).  status: 'included' | 'excluded_in_episode'
    | 'excluded_censored_horizon'.  time_to_onset_s is label-side metadata (never a feature)."""
    H = int(cfg.horizon_s * 1e9)
    eps = sorted(episodes, key=lambda e: e.onset_ns)
    out = []
    for t in times_ns:
        t = int(t)
        row = {"label": 0, "status": "included", "episode_event_id": "", "time_to_onset_s": float("nan")}
        if any(e.onset_ns <= t <= e.end_ns for e in eps):
            row.update(label=0, status="excluded_in_episode")
        elif observed_end_ns is None or t + H > observed_end_ns:
            row.update(label=0, status="excluded_censored_horizon")
        else:
            nxt = next((e for e in eps if t < e.onset_ns <= t + H), None)
            if nxt is not None:
                row.update(label=1, episode_event_id=nxt.event_id, time_to_onset_s=(nxt.onset_ns - t) / 1e9)
        out.append(row)
    return out
