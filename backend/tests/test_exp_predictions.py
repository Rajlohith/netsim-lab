"""Stage 7: Stage 6 predictions recorded next to the measurements, without future-data leakage."""
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.compare import analysis as A
from app.compare.campaign import CampaignConfig, execute
from app.compare.experiment import ROOT, ExperimentSpec
from app.compare.runner import run_experiment

MODEL = ROOT / "examples" / "ml" / "stage6" / "model"


def read_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _ml_ok():
    try:
        from app.ml.bundle import load_bundle
        load_bundle(MODEL)
        return True
    except Exception:
        return False


@unittest.skipUnless(_ml_ok(), "Stage 6 model bundle not loadable here (missing file or scikit-learn version mismatch)")
class Predictions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name) / "c"
        cfg = CampaignConfig.from_json({"name": "p", "replicates": 2, "approaches": ["mip-basic"], "duration_s": 40.0})
        cls.res = execute(cfg, cls.dir, predict_model=MODEL)
        cls.run_path = cls.dir / "runs" / "baseline__mip-basic" / "r000"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_predictions_are_saved_next_to_each_run_and_recorded_in_the_ledger(self):
        self.assertEqual(self.res["status_counts"], {"ok": 2})
        self.assertTrue((self.run_path / "handoff_predictions.csv").exists())
        self.assertTrue((self.run_path / "handoff_predictions.json").exists())
        ends = [json.loads(l) for l in (self.dir / "ledger.jsonl").read_text().splitlines() if '"end"' in l]
        self.assertTrue(all(e["prediction"]["ok"] for e in ends))
        self.assertTrue(all(e["prediction"]["run_in_training_split"] is False for e in ends))

    def test_every_prediction_uses_only_observations_up_to_its_decision_time(self):
        rows = read_rows(self.run_path / "handoff_predictions.csv")
        self.assertTrue(rows)
        for r in rows:
            if r["last_observation_ns"]:
                self.assertLessEqual(int(r["last_observation_ns"]), int(r["decision_time_ns"]))
            self.assertGreater(int(r["horizon_end_ns"]), int(r["decision_time_ns"]))

    def test_predictions_do_not_change_when_the_future_of_the_run_is_removed(self):
        """The strongest leakage check available: score the same decision times from the FULL run and from a copy truncated at a
        cut time; every prediction made at or before the cut must be identical."""
        from app.ml.bundle import load_bundle
        from app.ml.predict import HandoffPredictor
        from app.ml.runs import load_run
        run = load_run(self.run_path)
        pred = HandoffPredictor(load_bundle(MODEL), "random_forest")
        times = np.array([int(r["decision_time_ns"]) for r in read_rows(self.run_path / "handoff_predictions.csv")])
        cut = int(np.median(times))
        early = times[times <= cut]
        full = [p.score for p in pred.predict_run(run, early)]
        trunc = [p.score for p in pred.predict_run(run.truncated(cut), early)]
        self.assertGreater(len(early), 5)
        self.assertEqual(full, trunc)

    def test_annotation_does_not_touch_the_raw_measurements(self):
        with tempfile.TemporaryDirectory() as t:
            res = run_experiment(ExperimentSpec("mip-basic", duration_s=40.0), t)
            names = ("sent.csv", "echoes.csv", "received.csv", "events.csv", "approach_events.csv", "comparison_metrics.json")
            before = {n: hashlib.sha256((res.run_dir / n).read_bytes()).hexdigest() for n in names}
            from app.ml.integration import annotate_run
            annotate_run(res.run_dir, MODEL)
            after = {n: hashlib.sha256((res.run_dir / n).read_bytes()).hexdigest() for n in names}
            self.assertEqual(before, after)

    def test_analysis_joins_predictions_with_measured_outcomes(self):
        A.analyze(self.dir)
        runs = read_rows(self.dir / "analysis" / "runs.csv")
        self.assertTrue(all(r["pred_status"] == "ok" for r in runs))
        r = runs[0]
        self.assertGreaterEqual(int(r["pred_episodes"]), int(r["pred_episodes_warned"]))
        self.assertGreater(int(r["pred_decision_points"]), 0)
        h = read_rows(self.dir / "analysis" / "handoffs.csv")
        self.assertIn("pred_warned", h[0])
        self.assertTrue(any(x["pred_warned"] in ("true", "false") for x in h))
        ps = json.loads((self.dir / "analysis" / "predictions_summary.json").read_text())
        self.assertEqual(ps[0]["runs_in_model_training_split"], 0)
        self.assertIn("not evidence of skill", ps[0]["note"])

    def test_campaign_without_a_model_records_no_predictions(self):
        with tempfile.TemporaryDirectory() as t:
            execute(CampaignConfig.from_json({"name": "n", "replicates": 1, "approaches": ["mip-basic"]}), Path(t) / "c")
            A.analyze(Path(t) / "c")
            r = read_rows(Path(t) / "c" / "analysis" / "runs.csv")[0]
            self.assertEqual(r["pred_status"], "none")

    def test_predictor_failure_is_recorded_and_does_not_invalidate_the_measurement(self):
        with tempfile.TemporaryDirectory() as t:
            res = execute(CampaignConfig.from_json({"name": "n", "replicates": 1, "approaches": ["mip-basic"]}), Path(t) / "c",
                          predict_model=Path(t) / "no-such-model")
            self.assertEqual(res["status_counts"], {"ok": 1})
            led = [json.loads(l) for l in (Path(t) / "c" / "ledger.jsonl").read_text().splitlines() if '"end"' in l][0]
            self.assertFalse(led["prediction"]["ok"])
            A.analyze(Path(t) / "c")
            r = read_rows(Path(t) / "c" / "analysis" / "runs.csv")[0]
            self.assertEqual(r["pred_status"], "failed")
            self.assertTrue(r["app_stall_ms"])


if __name__ == "__main__":
    unittest.main()
