#!/usr/bin/env python3
"""Regenerate examples/mobileip/ from the EMULATED Mobile IP scenario (no root, no network, deterministic).

Each folder is a complete run directory: raw CSV/JSON + derived metrics (mip_metrics.json, metrics.json).
Run:  python scripts/generate_mobileip_examples.py
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.mobileip.analyze import analyze_mip_run, summary_text      # noqa: E402
from app.mobileip.mobility_sim import MobilitySim, MobilitySpec     # noqa: E402

OUT = ROOT / "examples" / "mobileip"


def cut_ha_then_restore(sim):
    for n in ("ha-ana", "ha-anb"):
        sim.sim.links[n].up = False
    sim.mn.rt_initial = int(0.1e9)
    sim.sim.at(sim.sim.clock.mono + int(0.35e9), lambda: setattr(sim.sim.links["ha-ana"], "up", True))


def no_renewal(sim):
    sim.mn.rereg_fraction = 100.0


SCENARIOS = [
    ("mip-handoff", dict(), None),
    ("mip-clock-skew-resync", dict(mn_clock_skew_s=3600.0, moves=(), deregister_at_end=False, duration_s=3.0), None),
    ("mip-wrong-key", dict(mn_key=b"x" * 32, moves=(), deregister_at_end=False, duration_s=3.0), None),
    ("mip-expiry-no-renewal", dict(lifetime_s=2, moves=(), deregister_at_end=False, duration_s=6.0), no_renewal),
    ("mip-ha-unreachable-recovery", dict(moves=(), deregister_at_end=False, duration_s=3.0, probe_start_s=1.0),
     cut_ha_then_restore),
]


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    for name, kw, hook in SCENARIOS:
        sim = MobilitySim(MobilitySpec(scenario_id=name, run_id="example-1", **kw), OUT)
        if hook:
            hook(sim)
        print(f"== {name}\n" + summary_text(analyze_mip_run(sim.run())) + "\n")


if __name__ == "__main__":
    main()
