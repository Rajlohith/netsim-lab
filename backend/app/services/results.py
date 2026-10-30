"""Reading what a finished experiment left on disk. Everything returned here is read from the run's own files; nothing is computed
from assumptions, and a missing file is reported as missing (never replaced by zeros or defaults)."""
from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path

from ..compare import events as ev
from .errors import NotFound, ResultsNotAvailable

ID_RE = re.compile(r"^exp-[0-9a-f]{16}$")
MAX_JSON_BYTES = 64 * 1024 * 1024
RESULT_NAME, PROGRESS_NAME, JOB_NAME, LOG_NAME = "result.json", "progress.json", "job.json", "worker.log"
PREDICTION_COLUMNS = ("experiment_id", "model_name", "model_version", "decision_time_ns", "horizon_end_ns", "horizon_s", "score", "alarm",
                      "threshold", "last_observation_ns")


def check_id(exp_id) -> str:
    if not isinstance(exp_id, str) or not ID_RE.match(exp_id):
        raise NotFound(f"no experiment {str(exp_id)[:40]!r}")
    return exp_id


def job_dir(experiments_dir: Path, exp_id: str) -> Path:
    """experiments/<id>; the id format is validated and the resolved path must stay inside experiments_dir."""
    check_id(exp_id)
    base = Path(experiments_dir).resolve()
    p = (base / exp_id).resolve()
    if p.parent != base:
        raise NotFound(f"no experiment {exp_id!r}")
    return p


def read_json(path: Path):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError(f"{path.name}: larger than {MAX_JSON_BYTES} bytes")
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def write_json_atomic(path: Path, obj) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_result(jd: Path) -> dict | None:
    try:
        r = read_json(jd / RESULT_NAME)
    except (ValueError, OSError):
        return None
    return r if isinstance(r, dict) and "outcome" in r else None


def read_progress(jd: Path) -> dict | None:
    try:
        r = read_json(jd / PROGRESS_NAME)
    except (ValueError, OSError):
        return None
    return r if isinstance(r, dict) else None


def run_dir_of(jd: Path, result: dict | None) -> Path | None:
    """The run directory the worker reported, only if it lies inside <job>/runs."""
    if not result or not result.get("run_dir"):
        return None
    runs = (jd / "runs").resolve()
    p = (jd / result["run_dir"]).resolve()
    return p if runs in p.parents and p.is_dir() else None


def run_dir_for(jd: Path, row: dict) -> Path:
    """The run directory of a COMPLETED experiment, or ResultsNotAvailable explaining why there is none."""
    if row["status"] != "completed":
        raise ResultsNotAvailable(f"experiment {row['id']} is {row['status']}: results exist only for completed experiments",
                                  details={"status": row["status"], "live_metrics_available": False,
                                           "hint": ("poll GET /api/experiments/{id} until the status is 'completed'; metrics are produced when the run finishes, "
                                                    "there are no partial or live metrics" if row["status"] in ("queued", "running") else None)})
    rd = run_dir_of(jd, read_result(jd))
    if rd is None:
        raise ResultsNotAvailable(f"experiment {row['id']} is marked completed but its run directory is missing (deleted or moved)", code="results_missing")
    return rd


def load_metrics(run_dir: Path) -> dict:
    m = read_json(Path(run_dir) / "comparison_metrics.json")
    if not isinstance(m, dict):
        raise ResultsNotAvailable("comparison_metrics.json is missing from the run directory", code="results_missing")
    return m


def provenance(metrics: dict) -> dict:
    p = metrics.get("provenance") or {}
    origin = p.get("data_origin", "unknown")
    return {"data_origin": origin, "is_live": False, "clock": p.get("clock"), "network": (p.get("network")),
            "timing_comparable_with_other_environments": p.get("timing_comparable_with_other_environments"),
            "analytic_values": p.get("analytic_values", []),
            "note": ("Produced by the discrete-event simulator in virtual time and read from files AFTER the run finished. Not live, not a real network."
                     if origin == "simulated" else
                     "Measured on loopback sockets of the server machine AFTER the run finished. Not live; not a real network path." if origin.startswith("real-kernel-loopback")
                     else "Read from files after the run finished. Not live.")}


def summarize(metrics: dict) -> dict:
    """Small summary copied from comparison_metrics.json for history rows (null stays null)."""
    hs, loss = metrics.get("handoff_summary") or {}, metrics.get("loss") or {}
    ov = metrics.get("mobility_overhead") or {}
    return {"handoff_count": hs.get("count"), "all_handoffs_recovered": (metrics.get("continuity") or {}).get("all_handoffs_recovered"),
            "handoff_latency_ms_mean": hs.get("handoff_latency_ms_mean"), "handoff_latency_ms_max": hs.get("handoff_latency_ms_max"),
            "service_gap_ms_mean": hs.get("service_gap_ms_mean"), "service_gap_ms_max": hs.get("service_gap_ms_max"),
            "loss_packets_total": hs.get("loss_packets_total"), "loss_percent": loss.get("percent"), "packets_lost": loss.get("lost"),
            "packets_sent": loss.get("sent"), "loss_is_exact": loss.get("exact"), "rtt_p50_ms": (metrics.get("rtt_ms") or {}).get("p50"),
            "goodput_bps": metrics.get("goodput_bps"), "mobility_overhead_bytes_per_packet": ov.get("mobility_bytes_per_packet"),
            "run_status": metrics.get("run_status"), "failure_events": (metrics.get("continuity") or {}).get("failure_events"),
            "data_origin": (metrics.get("provenance") or {}).get("data_origin")}


def read_approach_events(run_dir: Path, after_seq: int, limit: int):
    """(rows, next_after_seq or None, total) from approach_events.csv; None when the file does not exist."""
    rows = ev.read_events(Path(run_dir) / "approach_events.csv")
    if rows is None:
        return None
    rows = sorted(rows, key=lambda r: r["seq"])
    page = [r for r in rows if r["seq"] > after_seq][:limit]
    more = any(r["seq"] > (page[-1]["seq"] if page else after_seq) for r in rows) if page else False
    return page, (page[-1]["seq"] if more else None), len(rows)


def read_prediction_rows(run_dir: Path):
    p = Path(run_dir) / "handoff_predictions.csv"
    if not p.is_file():
        return None
    out = []
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            def num(k, conv):
                v = r.get(k)
                return None if v in (None, "") else conv(v)
            out.append({"experiment_id": r["experiment_id"], "model_name": r["model_name"], "model_version": r["model_version"],
                        "decision_time_ns": num("decision_time_ns", int), "horizon_end_ns": num("horizon_end_ns", int), "horizon_s": num("horizon_s", float),
                        "score": num("score", float), "alarm": bool(int(r["alarm"])), "threshold": num("threshold", float),
                        "last_observation_ns": num("last_observation_ns", int)})
    return out
