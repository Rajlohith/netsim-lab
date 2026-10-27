"""python -m app.mobileip.analyze RUN_DIR -> mip_metrics.json (+ Stage 3 metrics.json when probe data exists)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..traffic.analyze import analyze_run as analyze_traffic
from ..traffic.loader import read_json
from .events import read_events
from .metrics import compute_mip_metrics


def analyze_mip_run(run_dir, write=True) -> dict:
    d = Path(run_dir)
    events, notes = read_events(d)
    summary = read_json(d / "mip_summary.json")
    traffic = None
    if (d / "sent.csv").exists() or (d / "received.csv").exists():
        traffic = analyze_traffic(d, write=write)
    m = compute_mip_metrics(events, summary, traffic, notes)
    if write:
        (d / "mip_metrics.json").write_text(json.dumps(m, indent=2, sort_keys=True))
    return m


def summary_text(m: dict) -> str:
    if not m.get("available"):
        return "no Mobile IP data: " + "; ".join(m.get("warnings", []))
    r, t = m["registration_delay_ms"], m["tunnel"]
    out = [f"registrations: {r['count']} ok, delay ms min={r['min']} mean={r['mean']} max={r['max']}, retransmits={m['retransmissions']}",
           f"tunnel: overhead {t['overhead_bytes_per_packet']} B/packet ({t['overhead_percent_of_inner']}% of inner), "
           f"measured={t['measured']}"]
    for c in m["episodes"]:
        out.append(f"  attach {c['care_of_address']}: {c['outcome']} detection={c['detection_ms']}ms "
                   f"registered_after={c['time_to_registered_ms']}ms handoff_latency={c['handoff_latency_ms']}ms")
    for t_ in m["binding_timeline"]:
        out.append(f"  binding {t_['event']} -> reachable via {t_['reachable_via']}")
    if m["connectivity"]:
        for h in m["connectivity"]["handoffs"]:
            out.append(f"  {h['event_id']}: interruption={h['interruption_ms']}ms lost={h['loss_burst_packets']} pkts "
                       f"rtt {h['rtt_median_before_ms']}->{h['rtt_median_after_ms']}ms")
    f = {k: v for k, v in m["failures"].items() if isinstance(v, int) and v}
    out.append(f"failures: {f or 'none'}")
    out += [f"WARNING: {w}" for w in m["warnings"]]
    return "\n".join(out)


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m app.mobileip.analyze")
    p.add_argument("run_dir", type=Path)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        m = analyze_mip_run(a.run_dir)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(m, indent=2, sort_keys=True) if a.json else summary_text(m))
    return 0


if __name__ == "__main__":
    sys.exit(main())
