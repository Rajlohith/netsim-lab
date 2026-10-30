"""The ONLY place in the API that starts a process: the experiment worker.

Safety properties (asserted by tests/test_project_structure.py and tests/test_api_jobs.py):
  * argv is a fixed list built here - [python, -m, app.services.worker, <job dir>] - never a shell string, never client input;
  * shell=False, stdin closed, stdout/stderr go to the job's worker.log, a filtered environment (no API token, no secrets);
  * the worker is its own session/process group, so cancellation signals the whole group and never this server;
  * nothing here uses sudo or any privileged operation."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from .settings import ServiceSettings

BACKEND_DIR = Path(__file__).resolve().parents[2]          # the directory that contains the `app` package
WORKER_MODULE = "app.services.worker"


def worker_env(settings: ServiceSettings) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(BACKEND_DIR), "PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg",
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "TMPDIR": str(settings.tmp_dir)}
    for k in ("LANG", "LC_ALL", "HOME"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def default_launcher(job_dir: Path, settings: ServiceSettings) -> subprocess.Popen:
    argv = [sys.executable, "-m", WORKER_MODULE, str(job_dir)]
    with open(Path(job_dir) / "worker.log", "ab") as log:
        return subprocess.Popen(argv, cwd=str(job_dir), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=worker_env(settings),
                                close_fds=True, start_new_session=True, shell=False)


def signal_group(pid: int, sig: int) -> bool:
    """Signal the worker's process group. Refuses pid <= 1 and this server's own group. False if the group no longer exists."""
    if pid is None or pid <= 1:
        return False
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return False
    if pgid == os.getpgrp() or pgid <= 1:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False


def start_ticks(pid: int) -> int | None:
    """Process start time in clock ticks since boot (/proc/<pid>/stat field 22): with the pid it identifies one process, so a reused pid is detected."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat[stat.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def is_our_worker(pid: int, ticks: int | None, job_dir: Path) -> bool:
    """True only if `pid` is still the very process we launched for this job (same start time, worker module and job directory on its command line)."""
    if not pid or ticks is None or start_ticks(pid) != ticks:
        return False
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return WORKER_MODULE.encode() in cmd and str(job_dir).encode() in cmd


SIGTERM, SIGKILL = signal.SIGTERM, signal.SIGKILL
