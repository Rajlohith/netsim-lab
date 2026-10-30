#!/usr/bin/env python3
"""Run a few REAL experiments through the Stage 8 service layer (SQLite + worker processes) and save what the API returns.

    PYTHONPATH=backend python scripts/generate_api_examples.py            # writes examples/api/stage8/
    PYTHONPATH=backend python scripts/generate_api_examples.py --check    # re-runs into a temp dir and compares metrics.json byte-for-byte

Needs no web framework. Ids and timestamps differ from run to run (they are not reproducible, so they are only in session.json);
`metrics.json`/`handoffs.csv` (minus its experiment_id column) of a given request are deterministic (same configuration + seed -> same bytes), which --check verifies."""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.services.experiments import create_service  # noqa: E402
from app.services.settings import ServiceSettings  # noqa: E402

OUT = ROOT / "examples" / "api" / "stage8"
REQUESTS = [
    ("01-mip-basic-baseline", {"approach": "mip-basic", "scenario": "baseline", "seed": 1, "label": "example: Mobile IP baseline"}),
    ("02-mip-mbb-lossy-access-predict", {"approach": "mip-mbb", "handoff_mode": "mbb", "scenario": "lossy-access", "seed": 7, "predict": True, "label": "example: make-before-break under 2% loss, with predictions"}),
    ("03-tcp-reconnect-high-latency", {"approach": "tcp-reconnect", "scenario": "high-latency", "seed": 3, "label": "example: TCP reconnection, doubled delays"}),
    ("04-ro-model-distant-home-agent", {"approach": "ro-model", "scenario": "distant-home-agent", "seed": 5, "label": "example: route-optimisation model, distant Home Agent"}),
]


def without_id(csv_bytes: bytes) -> bytes:
    """Drop the first column (experiment_id: random per run) so the table is reproducible."""
    return b"\n".join(line.split(b",", 1)[1] if b"," in line else line for line in csv_bytes.splitlines()) + b"\n"


def run(out: Path) -> dict:
    work = Path(tempfile.mkdtemp(prefix="netsim-api-example-"))
    svc = create_service(ServiceSettings(data_dir=work, allow_root=True))      # allow_root: only so this can run in a root container; the CLI/server default refuses
    svc.start()
    session = {"note": "Real responses of the service layer behind the Stage 8 API; simulator-generated data (is_live=false). ids/timestamps vary per run.", "runs": []}
    try:
        for name, req in REQUESTS:
            created, _ = svc.create(req)
            while svc.get(created["id"])["status"] in ("queued", "running"):
                time.sleep(0.2)
            final = svc.get(created["id"])
            d = out / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "request.json").write_text(json.dumps(req, indent=2, sort_keys=True) + "\n")
            entry = {"name": name, "request": req, "status": final["status"], "error": final["error"], "response_on_create": created, "final_view": final}
            if final["status"] == "completed":
                m = svc.metrics(created["id"])
                (d / "metrics.json").write_text(json.dumps(m["metrics"], indent=2, sort_keys=True) + "\n")
                (d / "handoffs.csv").write_bytes(without_id(svc.export(created["id"], "csv", "handoffs")["content"]))
                if final["prediction"]["status"] == "available":
                    p = svc.predictions(created["id"], limit=10_000)
                    (d / "predictions_summary.json").write_text(json.dumps(p["summary"], indent=2, sort_keys=True) + "\n")
                entry["transitions"] = svc.transitions(created["id"])["items"]
            session["runs"].append(entry)
        (out / "history.csv").write_bytes(svc.export_history("csv")["content"])
        (out / "session.json").write_text(json.dumps(session, indent=2, sort_keys=True, default=str) + "\n")
    finally:
        svc.stop()
        shutil.rmtree(work, ignore_errors=True)
    return session


def main() -> int:
    if "--check" in sys.argv:
        tmp = Path(tempfile.mkdtemp(prefix="netsim-api-example-check-"))
        try:
            run(tmp)
            bad = []
            for name, _ in REQUESTS:
                for f in ("metrics.json", "handoffs.csv", "request.json"):
                    a, b = OUT / name / f, tmp / name / f
                    if not a.exists() or a.read_bytes() != b.read_bytes():
                        bad.append(f"{name}/{f}")
            print("reproduced byte-for-byte" if not bad else f"MISMATCH: {bad}")
            return 1 if bad else 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    s = run(OUT)
    print("wrote", OUT, [(r["name"], r["status"]) for r in s["runs"]])
    return 0 if all(r["status"] == "completed" for r in s["runs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
