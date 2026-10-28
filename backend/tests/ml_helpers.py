"""Synthetic fixtures for the ML unit tests. SYNTHETIC BY DESIGN: used only to test code paths; never used for reported results."""
import numpy as np

from app.ml.runs import Handoff, RunData

S = 1_000_000_000
MS = 1_000_000


def synthetic_run(*, duration_s=20, interval_ms=10, rtt_ms=30, handoffs=((10.0, 10.1),), gap=((10.0, 10.2),), approach="mip-basic",
                  events=(), scenario_end=True, experiment_id="exp/r1", scenario_id="exp"):
    """Probes every interval; echoes after rtt except those whose ARRIVAL falls in a `gap` interval (seconds)."""
    base = 100 * S
    sent = base + np.arange(0, int(duration_s * 1000 / interval_ms)) * interval_ms * MS
    echo = {}
    for i, t in enumerate(sent):
        r = int(t + rtt_ms * MS)
        if any(base + int(a * S) <= r < base + int(b * S) for a, b in gap):
            continue
        echo[i] = r
    hs = [Handoff(f"handoff-{k + 1}", base + int(a * S), (base + int(b * S)) if b is not None else None, "x") for k, (a, b) in enumerate(handoffs)]
    run = RunData.from_arrays(experiment_id=experiment_id, scenario_id=scenario_id, approach=approach, sent_ns=sent, echo_seq_recv_ns=echo,
                              handoffs=hs, events=[(base + int(t * S), k) for t, k in events], scenario_start_ns=base,
                              scenario_end_ns=(base + int(duration_s * S)) if scenario_end else None)
    run.base = base
    return run
