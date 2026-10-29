"""Stage 7: campaign design, reproducibility, failed / incomplete / interrupted runs, raw-data retention, resume."""
import hashlib
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path

from app.compare.approaches import APPROACHES
from app.compare.campaign import (CampaignConfig, CampaignError, Ledger, build_plan, draw_schedule, execute, replicate_seeds)
from app.compare.reproduce import reproduce
from app.compare.runner import run_experiment

CFG = {"name": "t", "replicates": 2, "approaches": ["mip-basic", "tcp-reconnect"], "sweeps": {"loss_percent": [2]}, "duration_s": 12.0}


def cfg(**kw):
    return CampaignConfig.from_json({**CFG, **kw})


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class Design(unittest.TestCase):
    def test_approaches_are_discovered_from_the_registry(self):
        plan = build_plan(CampaignConfig.from_json({"name": "d", "replicates": 1}))
        seen = {r.approach for r in plan.runs} | {c.approach for c in plan.cells}
        self.assertEqual(seen, set(APPROACHES))                            # nothing hard-coded, nothing silently dropped

    def test_unavailable_approach_is_reported_not_dropped(self):
        plan = build_plan(CampaignConfig.from_json({"name": "d", "replicates": 1}))
        quic = [c for c in plan.cells if c.approach == "quic-migration"]
        self.assertTrue(quic)
        self.assertTrue(all(c.status in ("unavailable", "unsupported") and c.reason for c in quic))

    def test_home_agent_factors_skip_approaches_without_a_home_agent(self):
        plan = build_plan(CampaignConfig.from_json({"name": "d", "replicates": 1, "sweeps": {"ha_distance_ms": [50], "registration_loss_percent": [10]}}))
        na = {(c.condition, c.approach) for c in plan.cells if c.status == "not_applicable"}
        self.assertIn(("ha-distance-ms-50", "tcp-reconnect"), na)
        self.assertIn(("registration-loss-percent-10", "quic-migration"), na)
        ran = {(r.condition, r.approach) for r in plan.runs}
        self.assertIn(("ha-distance-ms-50", "mip-basic"), ran)
        self.assertNotIn(("ha-distance-ms-50", "tcp-reconnect"), ran)

    def test_hold_factor_applies_only_to_mbb(self):
        plan = build_plan(CampaignConfig.from_json({"name": "d", "replicates": 1, "sweeps": {"old_link_hold_ms": [300]}}))
        self.assertEqual({r.approach for r in plan.runs if r.condition != "baseline"}, {"mip-mbb"})

    def test_common_random_numbers_across_approaches_and_conditions(self):
        plan = build_plan(cfg())
        by_rep = {}
        for r in plan.runs:
            by_rep.setdefault(r.replicate, set()).add((r.spec.seed, r.spec.conditions.seed, r.spec.moves))
        for k, s in by_rep.items():
            self.assertEqual(len(s), 1, f"replicate {k} must use one seed pair and one schedule everywhere")
        self.assertNotEqual(by_rep[0], by_rep[1])

    def test_schedules_vary_between_replicates_within_bounds(self):
        c = cfg(replicates=30)
        firsts = {draw_schedule(c, k)[0][0][0] for k in range(30)}
        self.assertGreater(len(firsts), 20)
        self.assertTrue(all(3.5 <= t <= 4.5 for t in firsts))
        self.assertEqual(draw_schedule(c, 3), draw_schedule(c, 3))
        self.assertEqual(replicate_seeds(1, 2), replicate_seeds(1, 2))
        self.assertNotEqual(replicate_seeds(1, 2), replicate_seeds(2, 2))

    def test_dwell_factor_changes_the_schedule(self):
        c = cfg(sweeps={"dwell_s": [1.0]})
        plan = build_plan(c)
        d = {(r.condition, r.replicate): r.schedule["dwell_s"] for r in plan.runs}
        self.assertTrue(all(0.75 <= d[("dwell-s-1", k)] <= 1.25 for k in range(2)))

    def test_config_validation(self):
        for bad in ({"replicates": 0}, {"approaches": ["nope"]}, {"sweeps": {"bogus": [1]}}, {"environment": "mars"},
                    {"sweeps": {"loss_percent": []}}, {"duration_s": 5.0}, {"surprise": 1}):
            with self.assertRaises(CampaignError, msg=bad):
                CampaignConfig.from_json({**CFG, **bad})

    def test_digest_changes_with_configuration(self):
        self.assertEqual(cfg().digest(), cfg().digest())
        self.assertNotEqual(cfg().digest(), cfg(replicates=3).digest())


class Execution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name) / "camp"
        cls.res = execute(cfg(), cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_planned_runs_finish_and_are_recorded(self):
        self.assertEqual(self.res["status_counts"], {"ok": 12})
        self.assertEqual(self.res["ran"], 12)
        led = Ledger(self.dir / "ledger.jsonl").read()
        self.assertEqual(sum(1 for r in led if r["event"] == "start"), 12)
        self.assertEqual(sum(1 for r in led if r["event"] == "end"), 12)

    def test_raw_data_experiment_config_and_meta_are_retained_for_every_run(self):
        plan = json.loads((self.dir / "plan.json").read_text())
        self.assertEqual(len(plan["runs"]), 12)
        for r in plan["runs"]:
            d = self.dir / "runs" / r["scenario_id"] / r["run_id"]
            for n in ("sent.csv", "echoes.csv", "received.csv", "events.csv", "approach_events.csv", "experiment.json",
                      "comparison_metrics.json", "campaign_run.json", "sender.meta.json"):
                self.assertTrue((d / n).exists(), f"{d.name}/{n}")
            exp = json.loads((d / "experiment.json").read_text())
            self.assertEqual(exp["spec"]["seed"], r["spec"]["seed"])
            self.assertIn("software", exp)
            self.assertIn("generated_wall_iso", exp)
            meta = json.loads((d / "campaign_run.json").read_text())
            self.assertEqual((meta["replicate"], meta["config_id"]), (r["replicate"], r["config_id"]))

    def test_campaign_json_records_config_hash_and_software(self):
        c = json.loads((self.dir / "campaign.json").read_text())
        self.assertEqual(c["config_sha256"], cfg().digest())
        self.assertIn("numpy", c["software"])

    def test_resume_does_not_rerun_finished_runs(self):
        calls = []
        r = execute(cfg(), self.dir, runner=lambda s, o, *a: calls.append(s) or run_experiment(s, o))
        self.assertEqual((r["ran"], r["skipped_ok"], calls), (0, 12, []))

    def test_other_configuration_cannot_reuse_the_directory(self):
        with self.assertRaises(CampaignError):
            execute(cfg(replicates=3), self.dir)

    def test_reproduction_is_byte_identical(self):
        rep = reproduce(self.dir, sample=5)
        self.assertEqual((rep["checked"], rep["identical"], rep["mismatches"]), (5, 5, []))

    def test_second_campaign_from_same_config_gives_identical_raw_files(self):
        with tempfile.TemporaryDirectory() as t2:
            execute(cfg(), Path(t2) / "again")
            plan = json.loads((self.dir / "plan.json").read_text())
            for r in plan["runs"]:
                for n in ("sent.csv", "echoes.csv", "received.csv", "events.csv", "comparison_metrics.json"):
                    a = self.dir / "runs" / r["scenario_id"] / r["run_id"] / n
                    b = Path(t2) / "again" / "runs" / r["scenario_id"] / r["run_id"] / n
                    self.assertEqual(sha(a), sha(b), f"{r['key']}/{n}")

    def test_tampered_raw_data_is_detected_by_reproduce(self):
        with tempfile.TemporaryDirectory() as t:
            import shutil
            c = Path(t) / "c"
            shutil.copytree(self.dir, c)
            key = json.loads((c / "plan.json").read_text())["runs"][0]
            p = c / "runs" / key["scenario_id"] / key["run_id"] / "echoes.csv"
            p.write_text(p.read_text() + "999999,1,1,1\n")
            rep = reproduce(c, sample=None, keys=[key["key"]])
            self.assertEqual(len(rep["mismatches"]), 1)
            self.assertIn("echoes.csv", rep["mismatches"][0]["differing_files"])


class Failures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "camp"
        self.cfg = cfg(approaches=["mip-basic"], sweeps={}, replicates=4)
        self.keys = [r.key for r in build_plan(self.cfg).runs]

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_run_is_recorded_with_traceback_and_others_continue(self):
        def runner(spec, out):
            if spec.run_id == "r001":
                raise RuntimeError("boom")
            return run_experiment(spec, out)
        r = execute(self.cfg, self.dir, runner=runner)
        self.assertEqual(r["status_counts"], {"ok": 3, "failed": 1})
        d = self.dir / "runs" / "baseline__mip-basic" / "r001"
        self.assertIn("boom", (d / "traceback.txt").read_text())
        exp = json.loads((d / "experiment.json").read_text())
        self.assertEqual(exp["status"], "failed")
        self.assertIn("RuntimeError: boom", exp["reason"])

    def test_failed_run_inside_the_experiment_runner_is_recorded(self):
        import app.compare.runner as R
        orig = R._dispatch
        R._dispatch = lambda spec, out, live: (_ for _ in ()).throw(ValueError("adapter broke")) if spec.run_id == "r002" else orig(spec, out, live)
        try:
            r = execute(self.cfg, self.dir)
        finally:
            R._dispatch = orig
        self.assertEqual(r["status_counts"], {"ok": 3, "failed": 1})
        self.assertTrue((self.dir / "runs" / "baseline__mip-basic" / "r002" / "traceback.txt").exists())

    def test_interrupt_keeps_partial_data_and_resume_finishes_without_redoing_ok_runs(self):
        def runner(spec, out):
            if spec.run_id == "r002":
                d = Path(out) / spec.scenario_id / spec.run_id
                d.mkdir(parents=True)
                (d / "sent.csv").write_text("seq,half\n1,2\n")           # a run that had written something when it was killed
                raise KeyboardInterrupt("user pressed ctrl-c")
            return run_experiment(spec, out)
        r = execute(self.cfg, self.dir, runner=runner)
        self.assertTrue(r["interrupted"])
        self.assertEqual(r["status_counts"], {"ok": 2, "interrupted": 1, "not_run": 1})
        d = self.dir / "runs" / "baseline__mip-basic" / "r002"
        exp = json.loads((d / "experiment.json").read_text())
        self.assertEqual(exp["status"], "interrupted")
        self.assertEqual(exp["partial_raw_files"], ["sent.csv"])
        calls = []
        r2 = execute(self.cfg, self.dir, runner=lambda s, o, *a: calls.append(s.run_id) or run_experiment(s, o))
        self.assertEqual(calls, ["r002", "r003"])                          # r000/r001 untouched
        self.assertEqual(r2["status_counts"], {"ok": 4})
        att = list((self.dir / "_attempts" / "baseline__mip-basic").iterdir())
        self.assertEqual([a.name for a in att], ["r002.attempt1"])
        self.assertEqual((att[0] / "sent.csv").read_text(), "seq,half\n1,2\n")   # the interrupted attempt is kept, not deleted

    def test_sigterm_is_turned_into_a_recorded_interruption(self):
        def runner(spec, out):
            if spec.run_id == "r001":
                os.kill(os.getpid(), signal.SIGTERM)
            return run_experiment(spec, out)
        before = signal.getsignal(signal.SIGTERM)
        r = execute(self.cfg, self.dir, runner=runner)
        self.assertTrue(r["interrupted"])
        self.assertEqual(Ledger(self.dir / "ledger.jsonl").final_status()["baseline__mip-basic/r001"]["status"], "interrupted")
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)         # handler restored

    def test_crash_without_end_record_is_incomplete_and_is_rerun(self):
        execute(self.cfg, self.dir, max_runs=2)
        Ledger(self.dir / "ledger.jsonl").append({"event": "start", "key": self.keys[2]})      # process died right after this line
        st = Ledger(self.dir / "ledger.jsonl").final_status()
        self.assertEqual(st[self.keys[2]]["status"], "incomplete")
        from app.compare.analysis import collect
        rows = {r["key"]: r for r in collect(self.dir)["runs"]}
        self.assertEqual(rows[self.keys[2]]["status"], "incomplete")
        self.assertEqual(rows[self.keys[3]]["status"], "not_run")
        r = execute(self.cfg, self.dir)
        self.assertEqual(r["status_counts"], {"ok": 4})

    def test_torn_ledger_line_is_ignored(self):
        execute(self.cfg, self.dir, max_runs=1)
        with open(self.dir / "ledger.jsonl", "a") as f:
            f.write('{"event": "start", "key": "x')                      # power loss mid-write
        self.assertEqual(Ledger(self.dir / "ledger.jsonl").final_status()[self.keys[0]]["status"], "ok")

    def test_failed_runs_are_not_retried_unless_asked(self):
        n = []
        def runner(spec, out):
            n.append(spec.run_id)
            if spec.run_id == "r000":
                raise RuntimeError("always")
            return run_experiment(spec, out)
        execute(self.cfg, self.dir, runner=runner)
        n.clear()
        execute(self.cfg, self.dir, runner=runner)
        self.assertEqual(n, [])
        execute(self.cfg, self.dir, runner=runner, retry_failed=True)
        self.assertEqual(n, ["r000"])
        self.assertTrue((self.dir / "_attempts" / "baseline__mip-basic" / "r000.attempt1").exists())

    def test_missing_metrics_file_on_an_ok_ledger_entry_becomes_incomplete(self):
        execute(self.cfg, self.dir)
        (self.dir / "runs" / "baseline__mip-basic" / "r001" / "comparison_metrics.json").unlink()
        from app.compare.analysis import collect
        rows = {r["key"]: r for r in collect(self.dir)["runs"]}
        self.assertEqual(rows[self.keys[1]]["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
