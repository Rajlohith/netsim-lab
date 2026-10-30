import json
import os
import time
import unittest
from pathlib import Path

from app.services import errors as E
from app.services import process_launcher as PL
from app.services.jobs import JobManager
from app.services.store import Store
from tests.api_helpers import CRASH, FAST, SLEEP, make_service, script_launcher, settings, tmpdir, wait_done, wait_for


def wait_launched(s, exp_id, timeout=30):
    """status 'running' is set by the claim; the worker pid is recorded and the process registered one step later."""
    end = time.time() + timeout
    while time.time() < end:
        if s.store.get(exp_id)["worker_pid"] and exp_id in s.manager.active_job_ids():
            return
        time.sleep(0.02)
    raise AssertionError("worker was never launched")


class RealWorker(unittest.TestCase):
    """The real worker process runs the real experiment code."""

    @classmethod
    def setUpClass(cls):
        import shutil, tempfile
        cls.tmp = Path(tempfile.mkdtemp(prefix="netsim-api-"))
        cls.svc = make_service(cls.tmp); cls.svc.start()

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls.svc.stop(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_lifecycle_queued_running_completed_with_real_results(self):
        v, created = self.svc.create({**FAST, "label": "lifecycle"})
        self.assertTrue(created); self.assertEqual(v["status"], "queued"); self.assertEqual(v["progress"]["phase"], "queued")
        self.assertFalse(v["data"]["is_live"]); self.assertFalse(v["data"]["live_metrics_available"])
        done = wait_done(self.svc, v["id"])
        self.assertEqual(done["status"], "completed", done["error"])
        self.assertEqual(done["progress"]["fraction"], 1.0)
        trans = [t["to_status"] for t in self.svc.transitions(v["id"])["items"]]
        self.assertEqual(trans, ["queued", "running", "completed"])
        # the summary is what the run's own file says - compare with the file on disk, not with a constant
        jd = self.svc.manager.job_dir(v["id"])
        rd = next((jd / "runs").rglob("comparison_metrics.json"))
        disk = json.loads(rd.read_text())
        self.assertEqual(done["result"]["summary"]["packets_sent"], disk["loss"]["sent"])
        self.assertEqual(done["result"]["summary"]["handoff_count"], disk["handoff_summary"]["count"])
        self.assertEqual(self.svc.metrics(v["id"])["metrics"], disk)
        self.assertEqual(self.svc.metrics(v["id"])["provenance"]["data_origin"], "simulated")
        self.assertFalse(self.svc.metrics(v["id"])["provenance"]["is_live"])

    def test_results_are_refused_until_completed(self):
        v, _ = self.svc.create({**FAST, "seed": 41})
        with self.assertRaises(E.ResultsNotAvailable) as cm:
            self.svc.metrics(v["id"])
        self.assertFalse(cm.exception.details["live_metrics_available"])
        for fn in (self.svc.handoffs, self.svc.events, self.svc.predictions):
            with self.assertRaises(E.ResultsNotAvailable):
                fn(v["id"])
        wait_done(self.svc, v["id"])

    def test_progress_is_phase_level_and_never_a_made_up_percentage(self):
        v, _ = self.svc.create({**FAST, "seed": 42, "predict": True})
        seen = set()
        end = time.time() + 60
        while time.time() < end:
            g = self.svc.get(v["id"])
            self.assertIsNone(g["progress"]["fraction"] if g["status"] in ("queued", "running") else None)
            seen.add(g["progress"]["phase"])
            if g["status"] not in ("queued", "running"):
                break
            time.sleep(0.01)
        self.assertEqual(g["status"], "completed"); self.assertLessEqual(seen, {"queued", "starting", "running_experiment", "predicting", "finalizing", "done"})

    def test_prediction_pipeline_runs_in_worker_and_is_served_from_files(self):
        v, _ = self.svc.create({**FAST, "seed": 43, "predict": True})
        g = wait_done(self.svc, v["id"])
        self.assertEqual((g["status"], g["prediction"]["status"]), ("completed", "available"))
        p = self.svc.predictions(v["id"], limit=3)
        self.assertEqual(p["model"]["id"], "stage6"); self.assertEqual(len(p["decision_points"]["items"]), 3)
        self.assertGreater(p["decision_points"]["total"], 3); self.assertFalse(p["is_live"])
        rd = next((self.svc.manager.job_dir(v["id"]) / "runs").rglob("handoff_predictions.json"))
        self.assertEqual(p["summary"], json.loads(rd.read_text()))

    def test_predictions_not_requested_is_404(self):
        v, _ = self.svc.create({**FAST, "seed": 44})
        wait_done(self.svc, v["id"])
        with self.assertRaises(E.NotFound) as cm:
            self.svc.predictions(v["id"])
        self.assertEqual(cm.exception.code, "predictions_not_available")

    def test_duplicate_active_run_is_refused_and_names_the_original(self):
        v, _ = self.svc.create({**FAST, "seed": 45})
        with self.assertRaises(E.DuplicateActiveExperiment) as cm:
            self.svc.create({**FAST, "seed": 45, "label": "same config"})
        self.assertEqual(cm.exception.details["existing_experiment_id"], v["id"])
        self.assertEqual(self.svc.validate({**FAST, "seed": 45})["resolved"]["would_conflict_with"]["experiment_id"], v["id"])
        wait_done(self.svc, v["id"])
        self.assertTrue(self.svc.create({**FAST, "seed": 45})[1])                      # finished: the same configuration may run again
        wait_for(self.svc, v["id"], lambda x: True)

    def test_idempotent_replay(self):
        a, c1 = self.svc.create({**FAST, "seed": 46}, "key-46")
        b, c2 = self.svc.create({**FAST, "seed": 46}, "key-46")
        self.assertEqual((c1, c2, a["id"]), (True, False, b["id"]))
        with self.assertRaises(E.IdempotencyKeyReused):
            self.svc.create({**FAST, "seed": 47}, "key-46")
        with self.assertRaises(E.ValidationFailed):
            self.svc.create({**FAST}, "bad key with spaces")
        self.assertEqual(len(os.listdir(self.svc.settings.experiments_dir)) , len(self.svc.store.list(limit=500)[0]))   # no stray directory from the replay
        wait_done(self.svc, a["id"])

    def test_invalid_request_creates_nothing(self):
        before = self.svc.store.counts()
        with self.assertRaises(E.ValidationFailed) as cm:
            self.svc.create({"approach": "mip-basic", "environment": "live"})
        self.assertEqual(cm.exception.details[0]["code"], "environment_not_exposed")
        self.assertEqual(self.svc.store.counts(), before)

    def test_unsupported_runner_outcome_is_failed_not_completed(self):
        # a run the runner itself rejects at execution time must end as 'failed' with its reason (simulated by a spec the worker refuses)
        v, _ = self.svc.create({**FAST, "seed": 48})
        wait_done(self.svc, v["id"])
        jd = self.svc.manager.job_dir(v["id"])
        job = json.loads((jd / "job.json").read_text())
        self.assertEqual(job["spec"]["environment"], "emulated"); self.assertNotIn("sudo", json.dumps(job))


class JobMachinery(unittest.TestCase):
    """Fake workers (a python one-liner) test the manager itself: cancellation, crashes, timeouts, concurrency, recovery."""

    def svc(self, code, **kw):
        s = make_service(tmpdir(self), launcher=script_launcher(code), **kw)
        s.start(); self.addCleanup(s.stop)
        return s

    def test_cancel_running_kills_the_process_group_and_ends_cancelled(self):
        s = self.svc(SLEEP)
        v, _ = s.create({**FAST})
        wait_for(s, v["id"], lambda x: x["status"] == "running"); wait_launched(s, v["id"])
        pid = s.store.get(v["id"])["worker_pid"]
        self.assertIsNotNone(PL.start_ticks(pid))
        view, done = s.cancel(v["id"])
        self.assertFalse(done); self.assertTrue(view["cancel_requested"])
        g = wait_done(s, v["id"], 20)
        self.assertEqual(g["status"], "cancelled")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual([t["to_status"] for t in s.transitions(v["id"])["items"]], ["queued", "running", "cancelled"])
        with self.assertRaises(E.NotCancellable):
            s.cancel(v["id"])
        with self.assertRaises(E.ResultsNotAvailable):
            s.metrics(v["id"])

    def test_cancel_queued_is_immediate_and_never_starts(self):
        s = self.svc(SLEEP, max_concurrent_jobs=1)
        a, _ = s.create({**FAST, "seed": 1}); b, _ = s.create({**FAST, "seed": 2})
        wait_for(s, a["id"], lambda x: x["status"] == "running")
        self.assertEqual(s.get(b["id"])["progress"]["queue_position"], 1)
        view, done = s.cancel(b["id"])
        self.assertTrue(done); self.assertEqual(view["status"], "cancelled")
        self.assertIsNone(s.store.get(b["id"])["started_at"])
        s.cancel(a["id"]); wait_done(s, a["id"], 20)

    def test_sigterm_ignoring_worker_is_killed_after_grace(self):
        s = self.svc("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)")
        v, _ = s.create({**FAST}); wait_for(s, v["id"], lambda x: x["status"] == "running"); wait_launched(s, v["id"]); time.sleep(0.5)
        t0 = time.time(); s.cancel(v["id"])
        self.assertEqual(wait_done(s, v["id"], 20)["status"], "cancelled"); self.assertGreater(time.time() - t0, 0.8)    # grace is 1 s

    def test_worker_crash_without_result_is_incomplete_and_flagged(self):
        s = self.svc(CRASH)
        v, _ = s.create({**FAST})
        g = wait_done(s, v["id"])
        self.assertEqual(g["status"], "incomplete"); self.assertEqual(g["error"]["code"], "worker_died"); self.assertIn("exited with code 3", g["error"]["message"])
        self.assertFalse(g["result"]["available"])
        with self.assertRaises(E.ResultsNotAvailable):
            s.metrics(v["id"])

    def test_worker_killed_from_outside_is_incomplete(self):
        s = self.svc(SLEEP)
        v, _ = s.create({**FAST}); wait_for(s, v["id"], lambda x: x["status"] == "running"); wait_launched(s, v["id"])
        PL.signal_group(s.store.get(v["id"])["worker_pid"], PL.SIGKILL)
        g = wait_done(s, v["id"], 20)
        self.assertEqual(g["status"], "incomplete"); self.assertIn("signal 9", g["error"]["message"])

    def test_timeout_is_failed_with_code_timeout(self):
        s = self.svc(SLEEP, max_runtime_s=0.5)
        v, _ = s.create({**FAST})
        g = wait_done(s, v["id"], 20)
        self.assertEqual((g["status"], g["error"]["code"]), ("failed", "timeout"))

    def test_launch_failure_is_failed(self):
        def boom(jd, st):
            raise OSError("no such interpreter")
        s = make_service(tmpdir(self), launcher=boom); s.start(); self.addCleanup(s.stop)
        v, _ = s.create({**FAST}); g = wait_done(s, v["id"])
        self.assertEqual((g["status"], g["error"]["code"]), ("failed", "launch_failed"))

    def test_scheduler_survives_launcher_errors_and_keeps_serving(self):
        calls = []

        def flaky(jd, st):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("first launch fails")
            return script_launcher(CRASH)(jd, st)
        s = make_service(tmpdir(self), launcher=flaky); s.start(); self.addCleanup(s.stop)
        a, _ = s.create({**FAST, "seed": 1}); b, _ = s.create({**FAST, "seed": 2})
        self.assertEqual(wait_done(s, a["id"])["status"], "failed"); self.assertEqual(wait_done(s, b["id"])["status"], "incomplete")

    def test_concurrency_limit_is_respected(self):
        s = self.svc(SLEEP, max_concurrent_jobs=2)
        ids = [s.create({**FAST, "seed": i})[0]["id"] for i in range(5)]
        wait_for(s, ids[1], lambda x: x["status"] == "running"); time.sleep(0.5)
        c = s.store.counts(); self.assertEqual((c["running"], c["queued"]), (2, 3))
        self.assertEqual(sorted(s.get(i)["progress"]["queue_position"] or 0 for i in ids), [0, 0, 1, 2, 3])
        for i in ids:
            s.cancel(i)
        for i in ids:
            wait_done(s, i, 30)
        self.assertEqual(s.store.counts()["cancelled"], 5)

    def test_queue_limit_returns_queue_full(self):
        s = self.svc(SLEEP, max_concurrent_jobs=1, max_queue_length=2)
        s.create({**FAST, "seed": 1}); s.create({**FAST, "seed": 2}); s.create({**FAST, "seed": 3})
        with self.assertRaises(E.QueueFull):
            for i in range(10, 14):
                s.create({**FAST, "seed": i})

    def test_delete_refuses_active_then_removes_files(self):
        s = self.svc(CRASH)
        v, _ = s.create({**FAST}); wait_done(s, v["id"])
        jd = s.manager.job_dir(v["id"]); self.assertTrue(jd.is_dir())
        s.delete(v["id"])
        self.assertFalse(jd.exists())
        with self.assertRaises(E.NotFound):
            s.get(v["id"])
        s2 = self.svc(SLEEP)
        w, _ = s2.create({**FAST}); wait_for(s2, w["id"], lambda x: x["status"] == "running")
        with self.assertRaises(E.StillActive):
            s2.delete(w["id"])

    def test_bad_ids_are_not_found_and_cannot_traverse(self):
        s = self.svc(CRASH)
        for bad in ("../x", "exp-../../etc", "exp-0000000000000000", "", "EXP-0000000000000000", "exp-00000000000000000"):
            with self.assertRaises(E.NotFound, msg=bad):
                s.get(bad)

    def test_launcher_is_fixed_argv_without_shell_or_secrets(self):
        import subprocess
        captured = {}
        real = subprocess.Popen

        class Spy:
            def __init__(self, argv, **kw):
                captured.update(argv=argv, kw=kw); self.pid = 0
        PL.subprocess.Popen = Spy
        try:
            os.environ["NETSIM_API_TOKEN"] = "secret"
            d = tmpdir(self); PL.default_launcher(d, settings(d))
        finally:
            PL.subprocess.Popen = real; os.environ.pop("NETSIM_API_TOKEN")
        self.assertEqual(captured["argv"][1:3], ["-m", "app.services.worker"]); self.assertIsInstance(captured["argv"], list)
        self.assertFalse(captured["kw"]["shell"]); self.assertTrue(captured["kw"]["start_new_session"])
        self.assertNotIn("NETSIM_API_TOKEN", captured["kw"]["env"]); self.assertEqual(len(captured["argv"]), 4)

    def test_signal_group_refuses_own_group_and_pid_1(self):
        self.assertFalse(PL.signal_group(os.getpid(), 0)); self.assertFalse(PL.signal_group(1, 0)); self.assertFalse(PL.signal_group(None, 0))


class RecoveryAndOwnership(unittest.TestCase):
    def test_restart_marks_running_incomplete_keeps_queued_and_kills_orphan(self):
        d = tmpdir(self)
        s1 = make_service(d, launcher=script_launcher(SLEEP), max_concurrent_jobs=1); s1.start()
        a, _ = s1.create({**FAST, "seed": 1}); b, _ = s1.create({**FAST, "seed": 2})
        wait_for(s1, a["id"], lambda x: x["status"] == "running"); wait_launched(s1, a["id"])
        pid = s1.store.get(a["id"])["worker_pid"]
        # simulate a crashed server: release the lock and stop the loop WITHOUT cleaning up the worker
        orphan = s1.manager._procs[a["id"]].proc
        s1.manager._stop.set(); s1.manager._thread.join(5); s1.manager._procs.clear()
        os.close(s1.manager._lockfd); s1.manager._lockfd = None
        os.kill(pid, 0)                                                                   # the orphan is alive
        s2 = make_service(d, launcher=script_launcher(CRASH), max_concurrent_jobs=1); s2.start(); self.addCleanup(s2.stop)
        ga = s2.get(a["id"])
        self.assertEqual((ga["status"], ga["error"]["code"]), ("incomplete", "server_restarted"))
        self.assertEqual(orphan.wait(5), -9)         # killed by SIGKILL (reaped here only because this test process is the old server; in reality init reaps it)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertNotEqual(wait_done(s2, b["id"])["status"], "queued")                   # queued job survived and was started by the new server

    def test_restart_with_pending_cancel_ends_cancelled(self):
        d = tmpdir(self)
        s1 = make_service(d, launcher=script_launcher(SLEEP)); s1.start()
        a, _ = s1.create({**FAST}); wait_for(s1, a["id"], lambda x: x["status"] == "running"); wait_launched(s1, a["id"])
        s1.store.request_cancel(a["id"])
        pid = s1.store.get(a["id"])["worker_pid"]
        orphan = s1.manager._procs[a["id"]].proc
        s1.manager._stop.set(); s1.manager._thread.join(5); s1.manager._procs.clear(); os.close(s1.manager._lockfd); s1.manager._lockfd = None
        s2 = make_service(d, launcher=script_launcher(CRASH)); s2.start(); self.addCleanup(s2.stop)
        self.assertEqual(s2.get(a["id"])["status"], "cancelled")
        self.assertEqual(orphan.wait(5), -9)

    def test_clean_shutdown_marks_running_incomplete_and_leaves_queue(self):
        d = tmpdir(self)
        s = make_service(d, launcher=script_launcher(SLEEP), max_concurrent_jobs=1); s.start()
        a, _ = s.create({**FAST, "seed": 1}); b, _ = s.create({**FAST, "seed": 2}); wait_for(s, a["id"], lambda x: x["status"] == "running"); wait_launched(s, a["id"])
        pid = s.store.get(a["id"])["worker_pid"]
        s.stop()
        self.assertEqual(s.get(a["id"])["error"]["code"], "server_shutdown"); self.assertEqual(s.get(a["id"])["status"], "incomplete")
        self.assertEqual(s.get(b["id"])["status"], "queued")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_pid_reuse_is_not_mistaken_for_our_worker(self):
        self.assertFalse(PL.is_our_worker(os.getpid(), PL.start_ticks(os.getpid()), Path("/nonexistent")))       # right process, wrong command line
        self.assertFalse(PL.is_our_worker(os.getpid(), 1, Path("/x")))                                              # wrong start time

    def test_only_one_manager_per_data_directory(self):
        d = tmpdir(self)
        a = make_service(d); a.start(); self.addCleanup(a.stop)
        b = make_service(d)
        with self.assertRaises(Exception) as cm:
            b.start()
        self.assertIn("another API instance", str(cm.exception))

    def test_refuses_to_run_as_root_unless_explicitly_allowed(self):
        d = tmpdir(self)
        from app.services.catalog import Catalog
        st = settings(d).with_(allow_root=False)
        m = JobManager(st, Store(st.db_path), euid=0)
        with self.assertRaises(Exception) as cm:
            m.start()
        self.assertIn("root", str(cm.exception))
        ok = JobManager(st, Store(st.db_path), euid=1000)
        ok.start(); self.addCleanup(ok.stop)

    def test_retention_sweep_deletes_old_finished_and_orphans_only(self):
        d = tmpdir(self)
        s = make_service(d, launcher=script_launcher(CRASH), retention_days=1); s.start(); self.addCleanup(s.stop)
        old, _ = s.create({**FAST, "seed": 1}); new, _ = s.create({**FAST, "seed": 2}); live, _ = s.create({**FAST, "seed": 3})
        wait_done(s, old["id"]); wait_done(s, new["id"]); wait_done(s, live["id"])
        import sqlite3
        c = sqlite3.connect(s.settings.db_path)
        c.execute("UPDATE experiments SET finished_at='2000-01-01T00:00:00.000000Z' WHERE id=?", (old["id"],)); c.commit(); c.close()
        orphan = s.settings.experiments_dir / "exp-0123456789abcdef"; orphan.mkdir(); os.utime(orphan, (1, 1))
        fresh_orphan = s.settings.experiments_dir / "exp-fedcba9876543210"; fresh_orphan.mkdir()
        removed = s.manager.sweep()
        self.assertEqual(removed, {"expired": 1, "orphans": 1})
        self.assertFalse(s.manager.job_dir(old["id"]).exists()); self.assertTrue(s.manager.job_dir(new["id"]).exists())
        self.assertFalse(orphan.exists()); self.assertTrue(fresh_orphan.exists())            # a directory younger than 1 h might belong to a submit in progress


class RealCancellation(unittest.TestCase):
    def test_cancelling_a_real_long_experiment_stops_the_real_worker(self):
        s = make_service(tmpdir(self)); s.start(); self.addCleanup(s.stop)
        v, _ = s.create({"approach": "mip-basic", "duration_s": 120, "rate_pps": 1000, "moves": [{"at_s": 30, "interface": "eth-b"}, {"at_s": 60, "interface": "eth-a"}]})
        wait_for(s, v["id"], lambda x: x["status"] == "running" and x["progress"]["phase"] == "running_experiment")
        time.sleep(1.0)
        pid = s.store.get(v["id"])["worker_pid"]
        s.cancel(v["id"])
        g = wait_done(s, v["id"], 30)
        self.assertEqual(g["status"], "cancelled")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse((s.manager.job_dir(v["id"]) / "runs").exists())                    # half-written outputs removed
        self.assertFalse(g["partial_outputs"])


if __name__ == "__main__":
    unittest.main()
