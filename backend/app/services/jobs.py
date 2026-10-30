"""Job manager: queue -> worker processes -> final state, with concurrency limits, safe cancellation, recovery and cleanup.

Lifecycle (status values): queued -> running -> completed | failed | cancelled | incomplete      (queued -> cancelled | failed also possible)
  completed   the worker finished and reported outcome 'ok'; results exist
  failed      the experiment code reported an error / unsupported / unavailable / invalid, the worker could not be launched, or it exceeded
              max_runtime_s (error_code 'timeout')
  cancelled   a client cancelled it (queued: immediately; running: the worker process group is SIGTERMed, SIGKILLed after cancel_grace_s)
  incomplete  the worker process ended WITHOUT writing a result (crashed, killed from outside, OOM) or the server restarted/shut down while
              it ran. Whatever it wrote is partial and is flagged as such; nothing from it is reported as a result.
A client cancelling an experiment that finishes first gets 'completed': the worker's own result file wins over the cancel flag.

Only ONE JobManager may own a data directory (flock on server.lock). Jobs run in separate processes; request handlers only enqueue."""
from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ConfigurationError, NotCancellable, NotFound, StillActive
from . import results as R
from .process_launcher import SIGKILL, SIGTERM, default_launcher, is_our_worker, signal_group, start_ticks
from .settings import ServiceSettings
from .store import ACTIVE, TERMINAL, Store

log = logging.getLogger("netsim.jobs")


@dataclass
class _Proc:
    proc: object
    started: float
    reason: str | None = None          # 'cancel' | 'timeout' | 'shutdown' - why WE signalled it
    term_at: float | None = None
    killed: bool = False


class JobManager:
    def __init__(self, settings: ServiceSettings, store: Store, *, launcher=None, euid: int | None = None, clock=time.monotonic):
        self.settings, self.store = settings, store
        self._launcher = launcher or default_launcher
        self._euid = os.geteuid() if euid is None else euid
        self._clock = clock
        self.instance_id = uuid.uuid4().hex[:12]
        self._cond = threading.Condition()
        self._procs: dict[str, _Proc] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lockfd: int | None = None
        self._last_sweep = float("-inf")

    # ---- paths -----------------------------------------------------------------------------------------------------------
    def job_dir(self, exp_id: str) -> Path:
        return R.job_dir(self.settings.experiments_dir, exp_id)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def active_job_ids(self) -> list[str]:
        with self._cond:
            return sorted(self._procs)

    # ---- lifecycle of the manager ----------------------------------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        if self._euid == 0 and not self.settings.allow_root:
            raise ConfigurationError("refusing to start the job manager as root: the project never runs the application as root "
                                     "(set NETSIM_API_ALLOW_ROOT=1 only inside a disposable container)")
        s = self.settings
        for d in (s.data_dir, s.experiments_dir, s.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)
        fd = os.open(s.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ConfigurationError(f"another API instance already owns {s.data_dir} (lock {s.lock_path}); run one API process per data directory") from None
        self._lockfd = fd
        self.store.init()
        self.recover()
        self.sweep()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="netsim-job-manager", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Stops scheduling, terminates running workers (their experiments become 'incomplete'), releases the data-directory lock.
        Queued experiments stay queued and run after the next start."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None
        self._shutdown_workers()
        if self._lockfd is not None:
            try:
                fcntl.flock(self._lockfd, fcntl.LOCK_UN)
            finally:
                os.close(self._lockfd)
            self._lockfd = None

    # ---- client operations -----------------------------------------------------------------------------------------------
    def submit(self, resolved, idempotency_key: str | None = None) -> tuple[dict, bool]:
        """Persist a validated experiment as 'queued' and wake the scheduler. Returns (row, created); created=False for an idempotent replay.
        Raises DuplicateActiveExperiment / IdempotencyKeyReused / QueueFull (nothing is left behind in that case)."""
        exp_id = "exp-" + secrets.token_hex(8)
        jd = self.job_dir(exp_id)
        jd.mkdir(parents=True)
        try:
            pred = None
            if resolved.predict:
                pred = {"model_dir": str(self.settings.model_dirs[resolved.predict["model"]]), "algorithm": resolved.predict["algorithm"]}
            R.write_json_atomic(jd / R.JOB_NAME, {"schema_version": 1, "experiment_id": exp_id, "spec": resolved.spec.to_json(), "predict": pred,
                                                  "limits": {"max_output_mb": self.settings.max_output_mb, "max_cpu_s": int(self.settings.max_runtime_s) + 30},
                                                  "allow_root": self.settings.allow_root})
            row, created = self.store.insert(exp_id=exp_id, resolved=resolved, idempotency_key=idempotency_key, max_queue=self.settings.max_queue_length)
        except BaseException:
            shutil.rmtree(jd, ignore_errors=True)
            raise
        if not created:
            shutil.rmtree(jd, ignore_errors=True)                 # replay: the original experiment's directory is the only one
        else:
            with self._cond:
                self._cond.notify_all()
        return row, created

    def cancel(self, exp_id: str) -> dict:
        """queued -> cancelled immediately; running -> cancel requested (the scheduler terminates the worker; poll the status);
        already finished -> NotCancellable (409)."""
        R.check_id(exp_id)
        row = self.store.get(exp_id)
        if row is None:
            raise NotFound(f"no experiment {exp_id!r}")
        if row["status"] in TERMINAL:
            raise NotCancellable(f"experiment {exp_id} is already {row['status']}", details={"status": row["status"]})
        row = self.store.request_cancel(exp_id)
        if row["status"] == "queued":
            self.store.transition(exp_id, "cancelled", expect=["queued"], note="cancelled by client before it started")
        with self._cond:
            self._cond.notify_all()
        return self.store.get(exp_id)

    def delete(self, exp_id: str) -> None:
        R.check_id(exp_id)
        row = self.store.get(exp_id)
        if row is None:
            raise NotFound(f"no experiment {exp_id!r}")
        if row["status"] in ACTIVE:
            raise StillActive(f"experiment {exp_id} is {row['status']}: cancel it first", details={"status": row["status"]})
        if self.store.delete(exp_id):
            shutil.rmtree(self.job_dir(exp_id), ignore_errors=True)

    # ---- scheduler -------------------------------------------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._reap()
                self._dispatch()
                if self._clock() - self._last_sweep > self.settings.sweep_interval_s:
                    self.sweep()
            except Exception:                                    # the scheduler must survive any single failure
                log.exception("job manager tick failed")
            with self._cond:
                if not self._stop.is_set():
                    self._cond.wait(self.settings.tick_s)

    def _dispatch(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                if len(self._procs) >= self.settings.max_concurrent_jobs:
                    return
            row = self.store.claim_next(self.instance_id)
            if row is None:
                return
            self._launch(row)

    def _launch(self, row: dict) -> None:
        exp_id = row["id"]
        jd = self.job_dir(exp_id)
        try:
            if not (jd / R.JOB_NAME).is_file():
                raise FileNotFoundError(f"{R.JOB_NAME} is missing from the job directory")
            proc = self._launcher(jd, self.settings)
            self.store.update(exp_id, worker_pid=proc.pid, worker_start_ticks=start_ticks(proc.pid))
        except Exception as e:
            log.exception("could not launch worker for %s", exp_id)
            self.store.transition(exp_id, "failed", expect=["running"], note="worker could not be launched", error_code="launch_failed",
                                  error_message=f"{type(e).__name__}: {e}")
            return
        with self._cond:
            self._procs[exp_id] = _Proc(proc, self._clock())

    def _reap(self) -> None:
        now = self._clock()
        with self._cond:
            items = list(self._procs.items())
        for exp_id, st in items:
            rc = st.proc.poll()
            if rc is not None:
                self._finalize(exp_id, st, rc)
                with self._cond:
                    self._procs.pop(exp_id, None)
                continue
            if st.reason is None:
                row = self.store.get(exp_id)
                if row is not None and row["cancel_requested"]:
                    st.reason, st.term_at = "cancel", now
                    signal_group(st.proc.pid, SIGTERM)
                elif now - st.started > self.settings.max_runtime_s:
                    st.reason, st.term_at = "timeout", now
                    signal_group(st.proc.pid, SIGTERM)
            if st.term_at is not None and not st.killed and now - st.term_at > self.settings.cancel_grace_s:
                st.killed = True
                signal_group(st.proc.pid, SIGKILL)

    def _finalize(self, exp_id: str, st: _Proc, rc: int) -> None:
        jd = self.job_dir(exp_id)
        result = R.read_result(jd)
        extra: dict = {}
        if result is not None and result.get("outcome") == "ok":
            run_dir = R.run_dir_of(jd, result)
            try:
                if run_dir is None:
                    raise ValueError("the worker reported success but its run directory is missing")
                metrics = R.load_metrics(run_dir)
                extra = {"summary_json": json.dumps(R.summarize(metrics), sort_keys=True), "run_dir": result["run_dir"]}
                to, note, fields = "completed", "finished", {}
            except Exception as e:
                to, note, fields = "failed", "results unreadable", {"error_code": "results_unreadable", "error_message": f"{type(e).__name__}: {e}"}
        elif result is not None:
            to, note = "failed", f"experiment reported {result.get('outcome')}"
            fields = {"error_code": str(result.get("outcome")), "error_message": str(result.get("reason") or "")[:2000]}
        elif st.reason == "cancel":
            to, note, fields = "cancelled", "cancelled by client while running", {}
        elif st.reason == "timeout":
            to, note, fields = "failed", "exceeded max_runtime_s", {"error_code": "timeout", "error_message": f"the experiment exceeded the {self.settings.max_runtime_s:g} s limit and was killed"}
        elif st.reason == "shutdown":
            to, note, fields = "incomplete", "server shut down while the experiment was running", {"error_code": "server_shutdown", "error_message": "the server stopped while this experiment was running"}
        else:
            how = f"was killed by signal {-rc}" if rc < 0 else f"exited with code {rc}"
            to, note, fields = "incomplete", "worker ended without a result", {"error_code": "worker_died", "error_message": f"the worker process {how} without writing a result"}
        pred = (result or {}).get("prediction") or {}
        row = self.store.get(exp_id)
        requested = bool(row and row["predict_model"])
        if requested:
            fields["prediction_status"] = (pred.get("status") or "failed") if to == "completed" else "not_run"
            fields["prediction_error"] = pred.get("reason") if to == "completed" else f"the experiment ended as {to}, so no predictions were computed"
            if to == "completed" and pred.get("summary") is not None:
                fields["prediction_summary_json"] = json.dumps(pred["summary"], sort_keys=True)
        fields.update(extra)
        fields["phase"] = None
        if to in ("cancelled", "failed", "incomplete"):
            fields["partial_outputs"] = self._cleanup_files(exp_id, remove_runs=(to == "cancelled"))
        if not self.store.transition(exp_id, to, expect=["running"], note=note, **fields):
            log.warning("could not move %s to %s (state changed underneath)", exp_id, to)

    # ---- recovery / cleanup ----------------------------------------------------------------------------------------------
    def recover(self) -> list[str]:
        """After a restart: every experiment recorded as running lost its supervisor. A surviving worker (verified by pid + start time + command
        line) is killed; the experiment becomes 'cancelled' if a cancel was pending, otherwise 'incomplete'. Queued experiments stay queued."""
        fixed = []
        for row in self.store.by_status("running"):
            jd = self.job_dir(row["id"])
            if row["worker_pid"] and is_our_worker(row["worker_pid"], row["worker_start_ticks"], jd):
                signal_group(row["worker_pid"], SIGKILL)
            result = R.read_result(jd)
            if result is not None and result.get("outcome") == "ok" and R.run_dir_of(jd, result) is not None:
                # the worker had finished before the restart: keep its result
                try:
                    metrics = R.load_metrics(R.run_dir_of(jd, result))
                    pred = result.get("prediction") or {}
                    self.store.transition(row["id"], "completed", expect=["running"], note="finished before the server restarted (recovered at start-up)",
                                          summary_json=json.dumps(R.summarize(metrics), sort_keys=True), run_dir=result["run_dir"], phase=None,
                                          prediction_status=pred.get("status", "not_requested"), prediction_error=pred.get("reason"),
                                          prediction_summary_json=None if pred.get("summary") is None else json.dumps(pred["summary"], sort_keys=True))
                    fixed.append(row["id"])
                    continue
                except Exception:
                    pass
            if row["cancel_requested"]:
                self.store.transition(row["id"], "cancelled", expect=["running"], note="cancel was pending when the server restarted", phase=None,
                                      partial_outputs=self._cleanup_files(row["id"], remove_runs=True))
            else:
                self.store.transition(row["id"], "incomplete", expect=["running"], note="server restarted while the experiment was running", phase=None,
                                      error_code="server_restarted", error_message="the server restarted while this experiment was running; its outputs are partial",
                                      partial_outputs=self._cleanup_files(row["id"], remove_runs=False))
            fixed.append(row["id"])
        return fixed

    def _cleanup_files(self, exp_id: str, *, remove_runs: bool) -> int:
        """Remove temp files; for cancelled experiments also the half-written run directory. For failed/incomplete the partial run directory is
        KEPT (diagnosis). Returns the `partial_outputs` flag to store. Runs BEFORE the terminal transition, so a client that sees the terminal
        status never sees leftovers or a stale flag."""
        jd = self.job_dir(exp_id)
        for tmp in jd.glob("*.tmp"):
            tmp.unlink(missing_ok=True)
        runs = jd / "runs"
        if remove_runs:
            shutil.rmtree(runs, ignore_errors=True)
            return 0
        return 1 if runs.is_dir() and any(runs.rglob("*")) else 0

    def sweep(self) -> dict:
        """Retention: delete finished experiments older than retention_days (0 = keep forever) and orphan directories without a database row."""
        self._last_sweep = self._clock()
        removed = {"expired": 0, "orphans": 0}
        s = self.settings
        if s.retention_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=s.retention_days)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for exp_id in self.store.older_than(cutoff):
                if self.store.delete(exp_id):
                    shutil.rmtree(self.job_dir(exp_id), ignore_errors=True)
                    removed["expired"] += 1
        if s.experiments_dir.is_dir():
            for child in s.experiments_dir.iterdir():
                if child.is_dir() and R.ID_RE.match(child.name) and self.store.get(child.name) is None and time.time() - child.stat().st_mtime > 3600:
                    shutil.rmtree(child, ignore_errors=True)
                    removed["orphans"] += 1
        return removed

    def _shutdown_workers(self) -> None:
        with self._cond:
            items = list(self._procs.items())
        for exp_id, st in items:
            st.reason = st.reason or "shutdown"
            signal_group(st.proc.pid, SIGTERM)
        deadline = self._clock() + self.settings.cancel_grace_s
        for exp_id, st in items:
            while st.proc.poll() is None and self._clock() < deadline:
                time.sleep(0.02)
            if st.proc.poll() is None:
                signal_group(st.proc.pid, SIGKILL)
                try:
                    st.proc.wait(5)
                except Exception:
                    pass
            self._finalize(exp_id, st, st.proc.poll() if st.proc.poll() is not None else -9)
            with self._cond:
                self._procs.pop(exp_id, None)
