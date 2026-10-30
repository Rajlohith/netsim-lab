"""Helpers for the Stage 8 API tests (service layer; no web framework needed)."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.services.experiments import create_service
from app.services.settings import ServiceSettings

FAST = {"approach": "mip-basic", "duration_s": 6.0, "moves": [{"at_s": 2.0, "interface": "eth-b"}, {"at_s": 4.0, "interface": "eth-a"}]}


def settings(tmp: Path, **kw) -> ServiceSettings:
    return ServiceSettings(data_dir=Path(tmp), allow_root=True, tick_s=0.05, cancel_grace_s=1.0, **kw).validate()


def make_service(tmp, launcher=None, **kw):
    return create_service(settings(tmp, **kw), launcher=launcher)


def tmpdir(test):
    d = Path(tempfile.mkdtemp(prefix="netsim-api-"))
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def wait_for(svc, exp_id, pred, timeout=90.0):
    end = time.time() + timeout
    while time.time() < end:
        v = svc.get(exp_id)
        if pred(v):
            return v
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting on {exp_id}: {svc.get(exp_id)['status']}")


def wait_done(svc, exp_id, timeout=90.0):
    return wait_for(svc, exp_id, lambda v: v["status"] not in ("queued", "running"), timeout)


def script_launcher(code: str):
    """A launcher that runs `python -c code` in its own session instead of the real worker (to test the job machinery itself)."""
    def launch(job_dir, settings):
        # the trailing "app.services.worker" argument is a marker: recovery only kills processes whose command line names the worker
        return subprocess.Popen([sys.executable, "-c", code, str(job_dir), "app.services.worker"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    return launch


SLEEP = "import time; time.sleep(600)"
CRASH = "import sys; sys.exit(3)"
