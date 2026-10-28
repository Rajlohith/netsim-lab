"""Integration tests: run the REAL emulator through the Stage 5 interface, build a dataset, train, save, reload, predict, evaluate.
Generated once per test class (about 15 short runs). Small sizes are test fixtures; reported results come from `python -m app.ml pipeline`."""
import hashlib
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from app.ml import evaluate as E
from app.ml.bundle import BundleError, load_bundle
from app.ml.config import MLConfig, MinData
from app.ml.datagen import default_families, generate, make_spec, sample_moves
from app.ml.dataset import build_dataset, load_dataset
from app.ml.features import FEATURE_NAMES, extract_features
from app.ml.integration import annotate_run
from app.ml.pipeline import run_pipeline
from app.ml.predict import HandoffPredictor
from app.ml.runs import discover_runs, load_run
from app.ml.splits import SPLITS, split_frames, group_split, assert_no_leakage

SMALL = MinData(min_groups_per_split=2, min_episodes_per_split=3, min_positive_examples_per_split=10, min_train_negatives=10)


class RealRunsBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ml_test_"))
        cls.raw = cls.tmp / "raw"
        fams = default_families(rates=(50.0,), detects=(20.0,), dwells=("uniform",))
        cls.manifest = generate(cls.raw, families=fams, seeds_per_family=3, duration_s=25.0, base_seed=7)
        cls.out = cls.tmp / "out"
        cls.results = run_pipeline(cls.raw, cls.out, by="experiment", seed=0, min_data=SMALL, make_plots=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class DatagenTests(RealRunsBase):
    def test_generation_is_deterministic_and_uses_the_existing_interface(self):
        a = make_spec(default_families()[0], 1, base_seed=5, family_index=0, duration_s=40.0)
        b = make_spec(default_families()[0], 1, base_seed=5, family_index=0, duration_s=40.0)
        self.assertEqual(a.to_json(), b.to_json())
        self.assertTrue(all(e["status"] == "ok" for e in self.manifest["runs"]), self.manifest["runs"])
        self.assertEqual(self.manifest["not_generated"][0]["approach"], "quic-migration")

    def test_moves_respect_tail_and_alternate(self):
        for dwell in ("uniform", "exponential"):
            mv = sample_moves(np.random.default_rng(1), dwell, 40.0, "eth-a")
            self.assertTrue(all(t <= 37.0 for t, _ in mv))
            self.assertEqual([i for _, i in mv][:2], ["eth-b", "eth-a"])
            self.assertTrue(all(b - a >= 1.4 for (a, _), (b, _) in zip(mv, mv[1:])))

    def test_same_run_twice_gives_identical_dataset(self):
        run_dir = discover_runs(self.raw)[0]
        from app.ml.dataset import build_run_examples
        a, _ = build_run_examples(load_run(run_dir), MLConfig())
        b, _ = build_run_examples(load_run(run_dir), MLConfig())
        pd.testing.assert_frame_equal(a, b)


class DatasetTests(RealRunsBase):
    def test_dataset_columns_and_no_label_leak_into_feature_list(self):
        ds = load_dataset(self.out / "dataset")
        self.assertTrue(set(FEATURE_NAMES) <= set(ds.df.columns))
        for bad in ("label", "time_to_onset_s", "status", "t_ns", "t_rel_s", "episode_event_id", "scenario_id", "experiment_id"):
            self.assertNotIn(bad, FEATURE_NAMES)

    def test_dataset_hash_is_verified(self):
        d = self.tmp / "ds_copy"
        shutil.copytree(self.out / "dataset", d)
        with open(d / "dataset.csv", "a") as f:
            f.write("tamper\n")
        with self.assertRaises(ValueError):
            load_dataset(d)

    def test_features_in_dataset_equal_recomputation_on_truncated_run(self):
        ds = load_dataset(self.out / "dataset")
        run = load_run(discover_runs(self.raw)[3])
        sub = ds.df[ds.df.experiment_id == run.experiment_id].sample(12, random_state=0)
        for _, row in sub.iterrows():
            t = int(row["t_ns"])
            v = extract_features(run.truncated(t), t, MLConfig().features).values
            for k in FEATURE_NAMES:
                x = row[k]
                self.assertTrue(math.isclose(x, v[k], rel_tol=1e-6, abs_tol=1e-6) or (pd.isna(x) and math.isnan(v[k])), (k, x, v[k]))

    def test_unavailable_features_are_empty_for_tcp_rows(self):
        df = load_dataset(self.out / "dataset").df
        tcp = df[df.approach == "tcp-reconnect"]
        self.assertGreater(len(tcp), 0)
        self.assertTrue(tcp["retransmit_count_long"].isna().all())
        self.assertFalse(df[df.approach == "mip-basic"]["retransmit_count_long"].isna().all())

    def test_class_imbalance_is_documented(self):
        cb = json.loads((self.out / "dataset" / "dataset_meta.json").read_text())["class_balance"]
        self.assertGreater(cb["negatives"], cb["positives"])
        self.assertIn("prevalence", cb)

    def test_raw_runs_are_not_modified_by_dataset_building(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.raw.rglob("*.csv")}
        build_dataset(self.raw, MLConfig())
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.raw.rglob("*.csv")}
        self.assertEqual(before, after)


class TrainingEvaluationTests(RealRunsBase):
    def test_pipeline_evaluated_and_artifacts_exist(self):
        r = self.results
        self.assertEqual(r["status"], "evaluated")
        for f in ("results.json", "results.csv", "RESULTS.md", "dataset/dataset.csv", "model/MANIFEST.json", "plots/pr_curves.png",
                  "plots/class_balance.png", "plots/test_metrics.png", "plots/warning_times.png", "plots/feature_importance.png"):
            self.assertTrue((self.out / f).exists(), f)
        self.assertEqual(set(r["models"]), {"threshold_baseline", "logreg", "random_forest"})

    def test_held_out_experiments_are_disjoint_from_training(self):
        sp = self.results["split"]["groups"]
        self.assertFalse(set(sp["train"]) & set(sp["test"]) or set(sp["train"]) & set(sp["validation"]) or set(sp["validation"]) & set(sp["test"]))
        ds = load_dataset(self.out / "dataset")
        fr = split_frames(ds.df, group_split(ds.df, by="experiment", seed=0, min_data=SMALL))
        assert_no_leakage(fr, "experiment")

    def test_reported_numbers_match_a_recomputation_from_the_saved_model(self):
        """Reload the bundle, re-score the held-out rows, recompute the confusion matrix: must equal results.json."""
        b = load_bundle(self.out / "model")
        df = load_dataset(self.out / "dataset").df
        test = df[df.experiment_id.isin(self.results["split"]["groups"]["test"])]
        inc = test[test.status == "included"]
        for name in ("logreg", "random_forest"):
            m = b.models[name]
            c = E.confusion(inc["label"], m.score(inc) >= m.threshold)
            rep = self.results["models"][name]["test"]
            self.assertEqual((c["tp"], c["fp"], c["tn"], c["fn"]), (rep["tp"], rep["fp"], rep["tn"], rep["fn"]))

    def test_metrics_are_internally_consistent(self):
        for n, m in self.results["models"].items():
            t = m["test"]
            self.assertEqual(t["tp"] + t["fn"], t["positives"])
            self.assertEqual(t["fp"] + t["tn"], t["negatives"])
            if t["precision"] is not None:
                self.assertTrue(0 <= t["precision"] <= 1)
            self.assertLessEqual(t["warning"]["episodes_detected"], t["warning"]["episodes_assessable"])

    def test_models_are_trained_only_on_training_groups(self):
        meta = json.loads((self.out / "model" / "training_metadata.json").read_text())
        self.assertEqual(meta["split"]["groups"]["train"], self.results["split"]["groups"]["train"])
        ds = load_dataset(self.out / "dataset").df
        n_train = int(ds.experiment_id.isin(meta["split"]["groups"]["train"]).sum())
        self.assertEqual(meta["train_rows"], n_train)

    def test_markdown_states_simulated_and_no_unearned_claims(self):
        md = (self.out / "RESULTS.md").read_text()
        self.assertIn("SIMULATED", md)
        self.assertIn("always-alarm reference", md)


class SerializationTests(RealRunsBase):
    def test_bundle_contents(self):
        mdir = self.out / "model"
        for f in ("feature_schema.json", "preprocessing.json", "label_definition.json", "thresholds.json", "metrics.json",
                  "training_metadata.json", "MANIFEST.json", "models/logreg.joblib", "models/random_forest.joblib", "models/threshold_baseline.json"):
            self.assertTrue((mdir / f).exists(), f)
        meta = json.loads((mdir / "training_metadata.json").read_text())
        for k in ("model_version", "libraries", "config", "dataset_sha256", "split", "seed", "class_balance"):
            self.assertIn(k, meta)
        self.assertIn("horizon_s", json.loads((mdir / "label_definition.json").read_text())["parameters"])

    def test_roundtrip_predictions_identical(self):
        b = load_bundle(self.out / "model")
        df = load_dataset(self.out / "dataset").df
        sample = df[df.status == "included"].sample(200, random_state=1)
        again = load_bundle(self.out / "model")
        for n in b.models:
            np.testing.assert_array_equal(b.models[n].score(sample), again.models[n].score(sample))
        self.assertEqual(b.models["random_forest"].threshold, self.results["models"]["random_forest"]["threshold"])

    def test_tampering_is_detected(self):
        d = self.tmp / "model_copy"
        shutil.copytree(self.out / "model", d)
        with open(d / "thresholds.json", "a") as f:
            f.write(" ")
        with self.assertRaises(BundleError):
            load_bundle(d)

    def test_schema_drift_is_detected(self):
        d = self.tmp / "model_drift"
        shutil.copytree(self.out / "model", d)
        sch = json.loads((d / "feature_schema.json").read_text())
        sch["features"][0]["name"] = "renamed"
        (d / "feature_schema.json").write_text(json.dumps(sch))
        mf = json.loads((d / "MANIFEST.json").read_text())
        import hashlib as h
        mf["files"]["feature_schema.json"] = h.sha256((d / "feature_schema.json").read_bytes()).hexdigest()
        (d / "MANIFEST.json").write_text(json.dumps(mf))
        with self.assertRaises(BundleError):
            load_bundle(d)


class PredictionTimestampTests(RealRunsBase):
    def test_prediction_timestamps_and_horizon(self):
        b = load_bundle(self.out / "model")
        run = load_run(discover_runs(self.raw)[0])
        pred = HandoffPredictor(b, "random_forest")
        t = run.first_probe_ns + 5_000_000_000
        p = pred.predict_at(run, t)
        self.assertEqual(p.decision_time_ns, t)
        self.assertEqual(p.horizon_end_ns - p.decision_time_ns, int(b.cfg.label.horizon_s * 1e9))
        self.assertLessEqual(p.last_observation_ns, p.decision_time_ns)
        self.assertEqual(p.model_version, b.model_version)
        self.assertEqual(p.alarm, p.score >= p.threshold)

    def test_prediction_unchanged_when_future_is_removed(self):
        b = load_bundle(self.out / "model")
        run = load_run(discover_runs(self.raw)[1])
        pred = HandoffPredictor(b, "logreg")
        for sec in (3.0, 6.0, 9.5, 14.0):
            t = run.first_probe_ns + int(sec * 1e9)
            self.assertEqual(pred.predict_at(run, t).score, pred.predict_at(run.truncated(t), t).score)

    def test_grid_is_monotonic(self):
        b = load_bundle(self.out / "model")
        run = load_run(discover_runs(self.raw)[0])
        ps = HandoffPredictor(b, "random_forest").predict_run(run)
        ts = [p.decision_time_ns for p in ps]
        self.assertEqual(ts, sorted(ts))
        self.assertTrue(all(b2 - a == int(b.cfg.sampling.step_s * 1e9) for a, b2 in zip(ts, ts[1:])))

    def test_unknown_model_rejected(self):
        with self.assertRaises(KeyError):
            HandoffPredictor(load_bundle(self.out / "model"), "nope")


class IntegrationTests(RealRunsBase):
    def test_annotate_adds_files_and_leaves_everything_else_untouched(self):
        run_dir = discover_runs(self.raw)[2]
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run_dir.iterdir() if p.is_file()}
        s = annotate_run(run_dir, self.out / "model", "random_forest")
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run_dir.iterdir() if p.is_file()}
        for k, v in before.items():
            self.assertEqual(after[k], v, k)
        self.assertEqual(set(after) - set(before), {"handoff_predictions.csv", "handoff_predictions.json"})
        rows = pd.read_csv(run_dir / "handoff_predictions.csv")
        self.assertEqual(len(rows), s["decision_points"])
        self.assertTrue((rows["horizon_end_ns"] > rows["decision_time_ns"]).all())
        self.assertIn("SIMULATED", s["provenance"])
        self.assertIn("run_in_training_split", s)

    def test_compare_cli_still_works_without_prediction_flag(self):
        from app.compare.cli import main
        out = self.tmp / "cmp"
        rc = main(["run", "--approach", "mip-basic", "--duration", "8", "--out-dir", str(out)])
        self.assertEqual(rc, 0)
        self.assertFalse(list(out.rglob("handoff_predictions.*")))


class PendingTests(unittest.TestCase):
    def test_insufficient_data_reports_pending_and_saves_nothing(self):
        tmp = Path(tempfile.mkdtemp(prefix="ml_pending_"))
        try:
            generate(tmp / "raw", families=default_families(rates=(50.0,), detects=(20.0,), dwells=("uniform",), configs=(("mip-bbm", "mip-basic", ()),)),
                     seeds_per_family=2, duration_s=15.0, base_seed=3)
            r = run_pipeline(tmp / "raw", tmp / "out", by="scenario", min_data=MinData())
            self.assertEqual(r["status"], "pending")
            self.assertTrue(r["pending_reasons"])
            self.assertNotIn("models", r)
            self.assertFalse((tmp / "out" / "model").exists())
            self.assertIn("EVALUATION PENDING", (tmp / "out" / "RESULTS.md").read_text())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_raw_dir_is_pending(self):
        tmp = Path(tempfile.mkdtemp(prefix="ml_empty_"))
        try:
            r = run_pipeline(tmp / "nothing", tmp / "out")
            self.assertEqual(r["status"], "pending")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
