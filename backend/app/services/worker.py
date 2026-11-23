"""Experiment worker: `python -m app.services.worker <job_dir>`. Runs in its OWN process so that long jobs never run inside request
handlers and can be cancelled by killing the process.

Protocol (files in <job_dir>, written by the server before launch / by this process):
  job.json       IN   resolved ExperimentSpec + limits (server-written; clients cannot reach it)
  progress.json  OUT  {"phase", "step", "steps_total", "at"}  (atomic replace)
  result.json    OUT  {"outcome", "reason", "run_dir", ...} written LAST and atomically; its absence means the process died
  runs/          OUT  the run directory written by app.compare.runner.run_experiment
  worker.log     OUT  stdout/stderr (tracebacks)
The worker only calls existing code: run_experiment. It never accepts a command or a path from
anywhere except job.json, refuses the privileged 'live' environment and refuses to run as root."""
from __future__ import annotations

import json
import os
import resource
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .results import JOB_NAME, PROGRESS_NAME, RESULT_NAME, write_json_atomic

ALLOWED_ENVIRONMENTS = ("emulated", "loopback")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _progress(jd: Path, phase: str, step: int, total: int) -> None:
    write_json_atomic(jd / PROGRESS_NAME, {"phase": phase, "step": step, "steps_total": total, "at": _now()})


def _finish(jd: Path, outcome: str, reason: str, **extra) -> int:
    write_json_atomic(jd / RESULT_NAME, {"schema_version": 1, "outcome": outcome, "reason": reason, "finished_at": _now(), **extra})
    return 0 if outcome == "ok" else 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m app.services.worker <job_dir>", file=sys.stderr)
        return 2
    jd = Path(argv[0]).resolve()
    try:
        job = json.loads((jd / JOB_NAME).read_text())
        if os.geteuid() == 0 and not job.get("allow_root"):
            return _finish(jd, "failed", "refusing to run an experiment as root (the project never runs the application as root)")
        lim = job.get("limits") or {}
        if lim.get("max_output_mb"):
            b = int(lim["max_output_mb"]) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (b, b))               # a runaway output raises OSError(EFBIG) instead of filling the disk
        if lim.get("max_cpu_s"):
            c = int(lim["max_cpu_s"])
            resource.setrlimit(resource.RLIMIT_CPU, (c, c + 5))

        from ..compare.experiment import ExperimentSpec                      # imported late: the heavy stack loads only in the worker
        from ..compare.runner import run_experiment
        spec = ExperimentSpec.from_json(job["spec"])
        if spec.environment not in ALLOWED_ENVIRONMENTS:
            return _finish(jd, "failed", f"environment {spec.environment!r} is not allowed in the API worker")
        total = 2
        _progress(jd, "running_experiment", 1, total)
        res = run_experiment(spec, jd / "runs")
        run_dir = None
        if res.run_dir and Path(res.run_dir).exists():
            run_dir = str(Path(res.run_dir).resolve().relative_to(jd))
        _progress(jd, "finalizing", total, total)
        return _finish(jd, "ok" if res.status == "ok" else res.status, res.reason or "", run_dir=run_dir)
    except BaseException as e:                                               # last resort: always leave a result so the server can tell 'failed' from 'died'
        traceback.print_exc()
        try:
            return _finish(jd, "failed", f"{type(e).__name__}: {e}")
        except Exception:
            return 3


if __name__ == "__main__":
    sys.exit(main())
