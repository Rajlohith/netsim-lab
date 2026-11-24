"""Loss study: WHY probes are 'lost' per approach, and what recovers them.   (SIMULATOR-GENERATED analysis, not a measurement.)

Two questions, answered from saved run files only:

1. TCP: its 'loss' is not packets lost on the wire. A reliable transport retransmits, so under random link loss a probe is
   only counted as lost when it is STILL UNDELIVERED when the run ends (a backlog stuck behind retransmission back-off).
   TcpLite has no fast retransmit / out-of-order buffering by default, so one lost segment stalls the whole stream until a
   (back-off) timeout. `run_tcp_recovery()` repeats the campaign for the TCP approaches with the opt-in RFC 5681 fast retransmit
   switched on (TcpLiteStack.FAST_RETRANSMIT_DEFAULT) so the two can be compared on identical seeds.
2. Mobile IP family: the loss is the traffic sent into the handoff BLACKOUT:   lost ~= rate_pps x blackout_s.
   `blackout_fit()` checks that against the per-handoff data (loss_packets vs interruption_ms).

Usage (from the repository root):
    PYTHONPATH=backend python3 -m app.compare.loss_study tcp-recovery --out-dir data/studies/tcp_recovery
    PYTHONPATH=backend python3 -m app.compare.loss_study blackout --campaign data/experiments/example
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import campaign as C
from .tcplite import TcpLiteStack


def tcp_config(loss_levels=(1, 2, 5), replicates=12, base_seed=20261028) -> C.CampaignConfig:
    return C.CampaignConfig.from_json({
        "name": "tcp-recovery", "base_seed": base_seed, "replicates": replicates, "duration_s": 12.0, "rate_pps": 100.0,
        "payload": 64, "environment": "emulated", "approaches": ["tcp-reconnect"],
        "schedule": {"first_move_s": [3.5, 4.5], "dwell_s": 3.0, "dwell_jitter_s": 0.25},
        "sweeps": {"loss_percent": list(loss_levels)}})


def run_tcp_recovery(out_dir, fast_retransmit: bool, replicates=12):
    """Run the TCP campaign with the opt-in loss recovery on or off (the flag is restored afterwards)."""
    old = TcpLiteStack.FAST_RETRANSMIT_DEFAULT
    TcpLiteStack.FAST_RETRANSMIT_DEFAULT = fast_retransmit
    try:
        return C.execute(tcp_config(replicates=replicates), out_dir)
    finally:
        TcpLiteStack.FAST_RETRANSMIT_DEFAULT = old


def blackout_fit(handoffs_csv) -> dict:
    """Least-squares through the origin of lost packets on blackout length, for runs without random link loss."""
    import pandas as pd
    h = pd.read_csv(handoffs_csv)
    h = h[(h.get("interruption_ms").notna()) & (h.get("loss_packets").notna())]
    x, y = h["interruption_ms"].to_numpy(float) / 1000.0, h["loss_packets"].to_numpy(float)
    if len(x) < 3:
        return {"n": int(len(x))}
    slope = float((x * y).sum() / (x * x).sum())
    r = float(np.corrcoef(x, y)[0, 1])
    return {"n_handoffs": int(len(x)), "fitted_packets_per_second": round(slope, 1), "pearson_r": round(r, 4)}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m app.compare.loss_study")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tcp-recovery")
    t.add_argument("--out-dir", required=True)
    t.add_argument("--replicates", type=int, default=12)
    b = sub.add_parser("blackout")
    b.add_argument("--handoffs-csv", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "tcp-recovery":
        out = Path(a.out_dir)
        res = {"baseline_model": run_tcp_recovery(out / "off", False, a.replicates),
               "with_fast_retransmit": run_tcp_recovery(out / "on", True, a.replicates)}
        print(json.dumps(res, indent=1, default=str))
    else:
        print(json.dumps(blackout_fit(a.handoffs_csv), indent=1))


if __name__ == "__main__":
    main()
