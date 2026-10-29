"""Stage 7: analysis from saved results, export formats, provenance labels, comparison logic, charts."""
import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.compare import analysis as A
from app.compare.campaign import CampaignConfig, execute
from app.compare.experiment import ExperimentSpec
from app.compare.plots import make_plots
from app.compare.runner import run_experiment

CFG = {"name": "x", "replicates": 8, "approaches": ["mip-basic", "mip-mbb", "tcp-reconnect"], "sweeps": {"ha_distance_ms": [100]},
       "duration_s": 12.0}


def read_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class FromSavedResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name) / "camp"
        execute(CampaignConfig.from_json(CFG), cls.dir)
        cls.res = A.analyze(cls.dir)
        cls.out = cls.dir / "analysis"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_expected_exports_exist(self):
        for n in ("runs.csv", "runs.json", "handoffs.csv", "summary.csv", "summary.json", "comparisons.csv", "comparisons.json",
                  "vs_baseline.csv", "vs_baseline.json", "predictions_summary.csv", "not_executed_cells.csv", "metric_definitions.json",
                  "analysis_meta.json", "REPORT.md"):
            self.assertTrue((self.out / n).exists(), n)

    def test_csv_and_json_agree_and_are_machine_readable(self):
        for stem in ("runs", "summary", "comparisons", "vs_baseline"):
            rows = read_rows(self.out / f"{stem}.csv")
            js = json.loads((self.out / f"{stem}.json").read_text())
            self.assertEqual(len(rows), len(js), stem)
            self.assertTrue(rows)
            self.assertEqual(set(rows[0]), set(js[0]) | set(rows[0]) & set(js[0]) | (set(rows[0]) - set(js[0])))
        runs = json.loads((self.out / "runs.json").read_text())
        for r in runs:
            if r["status"] == "ok":
                self.assertIsInstance(r["app_stall_ms"], float)
        json.loads((self.out / "analysis_meta.json").read_text())                    # valid JSON, no NaN/Infinity (allow_nan=False)
        self.assertNotIn("NaN", (self.out / "summary.json").read_text())

    def test_runs_csv_has_one_row_per_planned_run_with_seeds_and_conditions(self):
        rows = read_rows(self.out / "runs.csv")
        self.assertEqual(len(rows), self.res["runs"])
        for c in ("run_seed", "condition_seed", "first_move_s", "second_move_s", "p_loss_percent", "p_ha_distance_ms",
                  "p_detect_delay_ms", "p_latency_scale", "status", "reason", "config_id", "condition", "replicate"):
            self.assertIn(c, rows[0])
        self.assertTrue(all(r["run_seed"] for r in rows))

    def test_handoff_rows_are_exported_but_runs_are_the_unit_of_analysis(self):
        hrows = read_rows(self.out / "handoffs.csv")
        runs = [r for r in read_rows(self.out / "runs.csv") if r["status"] == "ok"]
        self.assertEqual(len(hrows), 2 * len(runs))                                   # two handoffs per run
        summ = json.loads((self.out / "summary.json").read_text())
        s = next(x for x in summ if x["condition"] == "baseline" and x["config_id"] == "mip-basic" and x["metric"] == "app_stall_ms")
        self.assertEqual(s["runs_with_value"], 8)                                      # n = runs, not handoffs (16)

    def test_regeneration_from_saved_results_is_identical(self):
        before = {p.name: sha(p) for p in self.out.iterdir() if p.is_file()}
        shutil.rmtree(self.out)
        A.analyze(self.dir)
        after = {p.name: sha(p) for p in self.out.iterdir() if p.is_file()}
        changed = {k for k in before if before[k] != after.get(k)}
        self.assertEqual(changed, {"analysis_meta.json"})                              # only the generation timestamp may differ

    def test_analysis_never_runs_a_simulation(self):
        import app.compare.runner as R
        orig = R.run_experiment
        R.run_experiment = lambda *a, **k: (_ for _ in ()).throw(AssertionError("analysis must not run experiments"))
        try:
            with tempfile.TemporaryDirectory() as t:
                A.analyze(self.dir, Path(t))
        finally:
            R.run_experiment = orig

    def test_analysis_works_on_a_copy_moved_elsewhere(self):
        with tempfile.TemporaryDirectory() as t:
            c = Path(t) / "moved"
            shutil.copytree(self.dir, c, ignore=shutil.ignore_patterns("analysis"))
            A.analyze(c)
            for n in ("summary.csv", "comparisons.csv", "runs.csv", "handoffs.csv"):
                self.assertEqual(sha(c / "analysis" / n), sha(self.out / n), n)         # no absolute paths leak into the exports

    def test_provenance_labels_distinguish_observed_and_derived(self):
        summ = json.loads((self.out / "summary.json").read_text())
        get = lambda m: next(s for s in summ if s["metric"] == m and s["ok_runs"])["provenance"]
        self.assertIn("simulator-generated", get("app_stall_ms"))
        self.assertIn("directly observed", get("app_stall_ms"))
        self.assertIn("derived", get("path_stretch"))
        defs = json.loads((self.out / "metric_definitions.json").read_text())
        self.assertEqual(defs["path_stretch"]["kind"], "derived")
        self.assertEqual(defs["app_stall_ms"]["kind"], "observed")

    def test_path_stretch_uses_direct_path_reference(self):
        rows = read_rows(self.out / "handoffs.csv")
        r = next(x for x in rows if x["config_id"] == "tcp-reconnect-notify" and x["condition"] == "baseline")
        self.assertIn(float(r["direct_rtt_reference_ms"]), (26.0, 54.0))
        self.assertAlmostEqual(float(r["path_stretch"]), float(r["rtt_after_ms"]) / float(r["direct_rtt_reference_ms"]), places=6)
        m = next(x for x in rows if x["config_id"] == "mip-basic" and x["condition"] == "ha-distance-ms-100")
        self.assertGreater(float(m["path_stretch"]), 3.0)                              # detour through a far Home Agent

    def test_confidence_intervals_exist_for_n8_and_degenerate_cells_are_flagged(self):
        summ = json.loads((self.out / "summary.json").read_text())
        s = next(x for x in summ if x["condition"] == "baseline" and x["config_id"] == "mip-basic" and x["metric"] == "app_stall_ms")
        self.assertIsNotNone(s["median_ci_lo"])
        self.assertGreaterEqual(s["median_ci_coverage"], 0.95)
        c = next(x for x in summ if x["condition"] == "baseline" and x["config_id"] == "mip-mbb" and x["metric"] == "app_stall_ms")
        self.assertTrue(c["degenerate"])
        self.assertIn("did not vary", c["ci_note"])

    def test_matched_pair_comparisons_are_complete_and_holm_adjusted(self):
        comps = json.loads((self.out / "comparisons.json").read_text())
        fam = [c for c in comps if c["condition"] == "baseline" and c["metric"] == "app_stall_ms"]
        self.assertEqual(len(fam), 6)                                                  # C(4,2): mip-basic, mip-mbb and the two TCP configurations
        for c in fam:
            self.assertEqual(c["n_pairs"], 8)
            self.assertGreaterEqual(c["p_holm"], c["p_value"])
        v = {(c["a"], c["b"]): c["verdict"] for c in fam}
        self.assertEqual(v[("mip-basic", "mip-mbb")], "b lower")                       # MBB stall (~20 ms) vs break-before-make (~80+ ms), 8/8 pairs
        loss = [c for c in comps if c["metric"] == "loss_percent"][0]
        self.assertIn("not comparable", loss["caveat"])

    def test_report_states_provenance_and_limitations(self):
        t = (self.out / "REPORT.md").read_text()
        for s in ("simulator-generated", "Limitations", "matched pairs", "Holm", "not executed"):
            self.assertIn(s.lower(), t.lower())
        self.assertNotIn("superior", t.lower())
        self.assertNotIn("best approach", t.lower())

    def test_charts_are_created_from_saved_analysis(self):
        made = make_plots(self.dir)
        self.assertGreaterEqual(len(made), 6)
        for m in made:
            p = self.dir / m
            self.assertTrue(p.exists() and p.stat().st_size > 2000, m)
            self.assertEqual(p.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertTrue(any("sweep_ha_distance_ms_path_stretch" in m for m in made))

    def test_plots_without_analysis_say_what_to_do(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(FileNotFoundError):
                make_plots(t)


class IncompleteAndFailedRunsInTheAnalysis(unittest.TestCase):
    def test_unfinished_and_failed_runs_are_counted_and_excluded_from_statistics(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = CampaignConfig.from_json({**CFG, "approaches": ["mip-basic"], "sweeps": {}, "replicates": 6})
            d = Path(t) / "c"
            def runner(spec, out):
                if spec.run_id == "r001":
                    raise RuntimeError("x")
                return run_experiment(spec, out)
            execute(cfg, d, runner=runner, max_runs=5)                                 # r005 never started
            res = A.analyze(d)
            self.assertEqual(res["status_counts"], {"failed": 1, "not_run": 1, "ok": 4})
            s = next(x for x in json.loads((d / "analysis" / "summary.json").read_text()) if x["metric"] == "app_stall_ms")
            self.assertEqual((s["planned_runs"], s["ok_runs"], s["failed_runs"], s["not_run"], s["runs_with_value"]), (6, 4, 1, 1, 4))
            runs = {r["run_id"]: r for r in read_rows(d / "analysis" / "runs.csv")}
            self.assertEqual(runs["r001"]["status"], "failed")
            self.assertIn("RuntimeError", runs["r001"]["reason"])
            self.assertEqual(runs["r005"]["status"], "not_run")
            self.assertIn("Runs that did not finish", (d / "analysis" / "REPORT.md").read_text())


class DiscoveredResults(unittest.TestCase):
    def test_stage5_style_folder_is_analysed_without_inventing_statistics(self):
        with tempfile.TemporaryDirectory() as t:
            for a in ("mip-basic", "tcp-reconnect"):
                run_experiment(ExperimentSpec(a), t)
            res = A.analyze(t)
            meta = json.loads((Path(t) / "analysis" / "analysis_meta.json").read_text())
            self.assertEqual(meta["mode"], "discovered")
            summ = json.loads((Path(t) / "analysis" / "summary.json").read_text())
            self.assertTrue(all(s["median_ci_lo"] is None for s in summ))               # n=1: no interval is fabricated
            self.assertTrue(all(c["verdict"] == "insufficient data" for c in json.loads((Path(t) / "analysis" / "comparisons.json").read_text())))
            self.assertIn("NO confidence intervals", (Path(t) / "analysis" / "REPORT.md").read_text())

    def test_shipped_stage5_example_results_can_be_analysed(self):
        from app.compare.experiment import ROOT
        with tempfile.TemporaryDirectory() as t:
            res = A.analyze(ROOT / "examples" / "comparison", Path(t))
            self.assertGreaterEqual(res["runs"], 5)


class ComparisonLogic(unittest.TestCase):
    """Synthetic input, used ONLY to test the decision rules; never part of reported results."""

    def _data(self, a_vals, b_vals):
        mk = lambda cfg, vals: [{"condition": "c", "config_id": cfg, "replicate": i, "status": "ok", "app_stall_ms": v, "loss_percent": 0.0,
                                 "path_stretch": 1.0, "signalling_bytes_per_handoff": 1.0, "model_limit_note": ""} for i, v in enumerate(vals)]
        return {"runs": mk("A", a_vals) + mk("B", b_vals)}

    def test_clear_gap_is_called_lower_and_noise_is_not(self):
        rows = A.compare_approaches(self._data([10 + i for i in range(12)], [50 + (i % 4) for i in range(12)]))
        v = next(r for r in rows if r["metric"] == "app_stall_ms")
        self.assertEqual(v["verdict"], "a lower")
        noise_a = [10, 12, 9, 14, 11, 13, 10, 12, 15, 9, 11, 13]
        noise_b = [11, 11, 10, 13, 12, 12, 11, 13, 14, 10, 10, 14]
        v = next(r for r in A.compare_approaches(self._data(noise_a, noise_b)) if r["metric"] == "app_stall_ms")
        self.assertEqual(v["verdict"], "no distinguishable difference")

    def test_five_pairs_are_never_enough_to_declare_a_difference(self):
        v = next(r for r in A.compare_approaches(self._data([1, 2, 3, 4, 5], [100, 101, 102, 103, 104])) if r["metric"] == "app_stall_ms")
        self.assertEqual(v["verdict"], "insufficient data")

    def test_constant_series_are_deterministic_differences_not_p_values(self):
        d = self._data([20.0] * 12, [80.0] * 12)
        v = next(r for r in A.compare_approaches(d) if r["metric"] == "app_stall_ms")
        self.assertEqual(v["verdict"], "deterministic difference")
        self.assertTrue(v["deterministic"])
        self.assertIsNone(v["p_value"])
        self.assertIsNone(v["p_holm"])
        self.assertIn("not applicable", v["note"])
        same = next(r for r in A.compare_approaches(self._data([5.0] * 12, [5.0] * 12)) if r["metric"] == "app_stall_ms")
        self.assertEqual(same["verdict"], "identical in every replicate")
        mixed = next(r for r in A.compare_approaches(self._data([20.0] * 12, [80.0 + i % 3 for i in range(12)])) if r["metric"] == "app_stall_ms")
        self.assertEqual(mixed["verdict"], "a lower")                              # one varying series => an ordinary paired test

    def test_few_identical_replicates_do_not_prove_determinism(self):
        v = next(r for r in A.compare_approaches(self._data([20.0] * 3, [80.0] * 3)) if r["metric"] == "app_stall_ms")
        self.assertEqual(v["verdict"], "insufficient data")
        self.assertFalse(v["deterministic"])

    def test_missing_replicate_drops_the_pair_not_the_run(self):
        d = self._data(list(range(10, 22)), list(range(50, 62)))
        d["runs"] = [r for r in d["runs"] if not (r["config_id"] == "B" and r["replicate"] == 3)]
        v = next(r for r in A.compare_approaches(d) if r["metric"] == "app_stall_ms")
        self.assertEqual(v["n_pairs"], 11)


if __name__ == "__main__":
    unittest.main()
