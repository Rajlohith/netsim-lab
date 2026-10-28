"""Generate controlled experiments for the dataset by running the EXISTING Stage 5 interface (compare.run_experiment) on the
Stage 4/5 emulator. Nothing is synthesised here except the experiment PARAMETERS (movement schedule, probe rate, detection
delay): every measurement in the result comes from the project's simulators.

Scenario family = approach configuration x probe rate x movement-detection delay x dwell-time model.
Within a family, runs differ only by the movement schedule (seeded RNG), so families are what splits must keep apart.

dwell models (how long the mobile node stays on one network before moving):
  uniform      dwell ~ U(2.5, 6.0) s          -> 'time since last handoff' carries real (scheduler-made) information
  exponential  dwell = 1.5 s + Exp(mean 2 s), capped at 8 s  -> close to memoryless: little to learn from handoff history
These are properties of how THIS experiment generator schedules moves; they are not claims about real mobility.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..compare.experiment import ExperimentSpec
from ..compare.runner import check, run_experiment

IFACES = ("eth-a", "eth-b")
DWELL_MODELS = ("uniform", "exponential")


@dataclass(frozen=True)
class Family:
    tag: str                       # short label for the approach configuration
    approach: str
    approach_kwargs: tuple = ()    # ExperimentSpec overrides as ((name, value), ...)
    rate_pps: float = 100.0
    detect_delay_ms: float = 20.0
    dwell: str = "uniform"

    @property
    def scenario_id(self) -> str:
        return f"ml-{self.tag}-r{int(self.rate_pps)}-d{int(self.detect_delay_ms)}-{self.dwell[:3]}"


APPROACH_CONFIGS = (
    ("tcp-evt", "tcp-reconnect", (("tcp_detect", "event"),)),
    ("tcp-tmo", "tcp-reconnect", (("tcp_detect", "timeout"),)),
    ("mip-bbm", "mip-basic", ()),
    ("mip-mbb", "mip-mbb", (("old_link_hold_ms", 100.0),)),
    ("ro", "ro-model", ()),
)


def default_families(*, rates=(50.0, 100.0), detects=(20.0, 100.0), dwells=DWELL_MODELS, configs=APPROACH_CONFIGS) -> list:
    return [Family(t, a, kw, r, d, w) for (t, a, kw) in configs for r in rates for d in detects for w in dwells]


def sample_moves(rng: np.random.Generator, dwell: str, duration_s: float, initial_iface: str, *, probe_start_s: float = 0.5,
                 tail_s: float = 3.0) -> tuple:
    """Alternating interface switches; the last move leaves >= tail_s for recovery and the prediction horizon."""
    if dwell not in DWELL_MODELS:
        raise ValueError(f"unknown dwell model {dwell!r}")
    moves, t, cur = [], probe_start_s, initial_iface
    while True:
        d = rng.uniform(2.5, 6.0) if dwell == "uniform" else 1.5 + min(rng.exponential(2.0), 6.5)
        t = round(t + float(d), 3)
        if t > duration_s - tail_s:
            break
        cur = IFACES[1] if cur == IFACES[0] else IFACES[0]
        moves.append((t, cur))
    return tuple(moves)


def make_spec(family: Family, k: int, *, base_seed: int, family_index: int, duration_s: float) -> ExperimentSpec:
    rng = np.random.default_rng(np.random.SeedSequence([base_seed, family_index, k]))
    initial = IFACES[int(rng.integers(0, 2))]
    moves = sample_moves(rng, family.dwell, duration_s, initial)
    return ExperimentSpec(approach=family.approach, environment="emulated", scenario_id=family.scenario_id, run_id=f"s{k}",
                          duration_s=duration_s, rate_pps=family.rate_pps, initial_iface=initial, moves=moves,
                          detect_delay_ms=family.detect_delay_ms, **dict(family.approach_kwargs))


def generate(raw_dir, *, families=None, seeds_per_family: int = 3, duration_s: float = 40.0, base_seed: int = 20260601,
             progress=None, skip_existing: bool = True) -> dict:
    """Run every (family, seed) experiment. Returns a manifest dict (also written to raw_dir/generation_manifest.json).
    Existing finished runs are kept (resumable); an incomplete run directory is deleted and rerun."""
    raw = Path(raw_dir)
    raw.mkdir(parents=True, exist_ok=True)
    families = families if families is not None else default_families()
    entries = []
    for fi, fam in enumerate(families):
        for k in range(seeds_per_family):
            spec = make_spec(fam, k, base_seed=base_seed, family_index=fi, duration_s=duration_s)
            run_dir = raw / spec.scenario_id / spec.run_id
            marker = run_dir / "experiment.json"
            reused = False
            if skip_existing and marker.exists():
                try:
                    prev = json.loads(marker.read_text())
                except ValueError:
                    prev = None
                if prev and prev.get("status") == "ok" and prev.get("spec", {}).get("moves") == [list(m) for m in spec.moves]:
                    status, reason, reused = "ok", "", True
            if not reused:
                if run_dir.exists():
                    if raw.resolve() not in run_dir.resolve().parents:
                        raise RuntimeError(f"refusing to delete {run_dir}: not inside {raw}")
                    shutil.rmtree(run_dir)
                res = run_experiment(spec, raw)
                status, reason = res.status, res.reason
                if status == "ok":
                    (run_dir / "ml_family.json").write_text(json.dumps({"tag": fam.tag, "dwell_model": fam.dwell, "rate_pps": fam.rate_pps,
                                                                        "detect_delay_ms": fam.detect_delay_ms, "base_seed": base_seed,
                                                                        "family_index": fi, "seed_index": k}, indent=2))
            entries.append({"scenario_id": spec.scenario_id, "run_id": spec.run_id, "family_index": fi, "status": status, "reason": reason,
                            "reused_existing": reused, "spec": spec.to_json()})
            if progress:
                progress(len(entries), len(families) * seeds_per_family, spec.scenario_id, spec.run_id, status)
    manifest = {"schema_version": 1, "tool": "app.ml.datagen", "base_seed": base_seed, "duration_s": duration_s,
                "seeds_per_family": seeds_per_family, "families": len(families), "runs": entries,
                "environment": "emulated (virtual time, deterministic): all measurements are SIMULATED",
                "not_generated": _not_generated()}
    (raw / "generation_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _not_generated() -> list:
    """Approaches deliberately absent from the dataset, with the live status/reason from the Stage 5 interface."""
    st, why = check(ExperimentSpec("quic-migration"))
    return [{"approach": "quic-migration", "status": st, "reason": why or "adapter never verified against real aioquic (PROJECT_STATUS K1); excluded from the dataset"}]
