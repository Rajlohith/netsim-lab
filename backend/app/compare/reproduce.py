"""Reproduction check: re-run planned runs of a saved campaign from plan.json (the exact saved specs, seeds and schedules) into a
scratch directory and compare the raw records and metrics byte for byte with the saved ones.

The emulator runs in virtual time with seeded random streams, so a correct reproduction is IDENTICAL (not merely close). Files with
real wall-clock stamps (experiment.json, ledger) are not compared. A mismatch means the code, the library versions or the saved
data differ from what produced the original results."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from .experiment import ExperimentSpec
from .runner import run_experiment

COMPARED = ("sent.csv", "echoes.csv", "received.csv", "events.csv", "approach_events.csv", "comparison_metrics.json")


def _h(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def reproduce(results_dir, sample: int | None = 10, keys: list | None = None) -> dict:
    root = Path(results_dir)
    plan = json.loads((root / "plan.json").read_text())
    ledger_ok = {json.loads(l)["key"] for l in (root / "ledger.jsonl").read_text().splitlines()
                 if l.strip() and json.loads(l).get("event") == "end" and json.loads(l).get("status") == "ok"}
    runs = [r for r in plan["runs"] if r["key"] in ledger_ok]
    if keys:
        runs = [r for r in runs if r["key"] in set(keys)]
    elif sample is not None and sample < len(runs):
        step = len(runs) / sample                      # evenly spread over conditions and approaches
        runs = [runs[int(i * step)] for i in range(sample)]
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for r in runs:
            spec = ExperimentSpec.from_json(r["spec"])
            res = run_experiment(spec, tmp)
            new_dir = Path(tmp) / spec.scenario_id / spec.run_id
            old_dir = root / "runs" / spec.scenario_id / spec.run_id
            diffs = [n for n in COMPARED if _h(new_dir / n) != _h(old_dir / n)]
            results.append({"key": r["key"], "status": res.status, "identical": res.status == "ok" and not diffs, "differing_files": diffs})
    return {"checked": len(results), "identical": sum(1 for x in results if x["identical"]),
            "mismatches": [x for x in results if not x["identical"]], "compared_files": list(COMPARED)}
