import unittest

import numpy as np
import pandas as pd

from app.ml import evaluate as E
from app.ml.models import ThresholdBaseline


class EvaluateTests(unittest.TestCase):
    def test_confusion_and_metrics(self):
        y = [1, 1, 1, 0, 0, 0, 0, 0]
        a = [1, 1, 0, 1, 0, 0, 0, 0]
        m = E.metrics_from_confusion(E.confusion(y, a))
        self.assertEqual((m["tp"], m["fp"], m["tn"], m["fn"]), (2, 1, 4, 1))
        self.assertAlmostEqual(m["precision"], 2 / 3)
        self.assertAlmostEqual(m["recall"], 2 / 3)
        self.assertAlmostEqual(m["f1"], 2 / 3)
        self.assertAlmostEqual(m["false_alarm_rate"], 1 / 5)

    def test_undefined_precision_is_none_not_zero(self):
        m = E.metrics_from_confusion(E.confusion([1, 0, 0], [0, 0, 0]))
        self.assertIsNone(m["precision"])
        self.assertEqual(m["recall"], 0.0)
        self.assertEqual(m["f1"], 0.0)

    def test_select_threshold_uses_given_scores_only(self):
        y = np.array([0, 0, 0, 1, 1, 1])
        s = np.array([.1, .2, .3, .7, .8, .9])
        t, why = E.select_threshold(y, s)
        self.assertTrue(0.3 < t <= 0.7, t)
        self.assertEqual(E.select_threshold(np.zeros(4), np.arange(4))[0], 0.5)

    def _frame(self):
        rows = []
        # experiment A: episode e1 onset at t=10; decision points every 0.25 s from 9.0 to 11.0
        for t in np.arange(8.0, 12.0, 0.25):
            lab = 1 if 9.0 <= t < 10.0 else 0
            rows.append({"experiment_id": "A", "t_ns": int(t * 1e9), "label": lab, "status": "included",
                         "episode_event_id": "e1" if lab else "", "time_to_onset_s": (10.0 - t) if lab else np.nan})
        return pd.DataFrame(rows)

    def test_warning_time_is_lead_of_first_alarm(self):
        df = self._frame()
        alarm = (df["t_ns"] >= int(9.5e9)) & (df["t_ns"] < int(10.0e9))
        w = E.warning_times(df, alarm.to_numpy())
        self.assertEqual((w["episodes_assessable"], w["episodes_detected"], w["episodes_missed"]), (1, 1, 0))
        self.assertAlmostEqual(w["warning_time_s"]["median"], 0.5)

    def test_alarm_before_horizon_is_not_credited_and_miss_is_counted(self):
        df = self._frame()
        early = (df["t_ns"] < int(9.0e9)).to_numpy()
        w = E.warning_times(df, early)
        self.assertEqual((w["episodes_detected"], w["episodes_missed"]), (0, 1))
        self.assertIsNone(w["warning_time_s"]["median"])

    def test_false_alarm_events_group_consecutive_alarms(self):
        df = self._frame()
        alarm = ((df["t_ns"] >= int(8.0e9)) & (df["t_ns"] < int(8.75e9))) | ((df["t_ns"] >= int(11.0e9)) & (df["t_ns"] < int(11.5e9)))
        self.assertEqual(E.alarm_bursts(df, alarm.to_numpy()), 2)

    def test_evaluate_split_ignores_excluded_rows(self):
        df = self._frame()
        df.loc[df.index[:4], "status"] = "excluded_in_episode"
        m = E.evaluate_split(df, np.ones(len(df)), 0.5, step_s=0.25)
        self.assertEqual(m["tp"] + m["fp"] + m["tn"] + m["fn"], len(df) - 4)

    def test_threshold_baseline_never_fires_on_nan_and_serialises(self):
        X = pd.DataFrame({"loss_rate_short": [0, 0, 0.5, 0.6, np.nan, 0.0], "jitter_ms": [0.0] * 6})
        y = np.array([0, 0, 1, 1, 1, 0])
        b = ThresholdBaseline(("loss_rate_short", "jitter_ms")).fit(X, y)
        self.assertEqual(b.feature, "loss_rate_short")
        p = b.predict_proba(X)[:, 1]
        self.assertEqual(p.tolist(), [0, 0, 1, 1, 0, 0])
        b2 = ThresholdBaseline.from_dict(b.to_dict())
        self.assertEqual(b2.predict_proba(X)[:, 1].tolist(), p.tolist())

    def test_degenerate_baseline_is_flagged(self):
        X = pd.DataFrame({"loss_rate_short": [0.0] * 6})
        b = ThresholdBaseline(("loss_rate_short",)).fit(X, np.array([0, 1, 0, 1, 0, 0]))
        self.assertTrue(b.degenerate)
        self.assertEqual(b.predict_proba(X)[:, 1].sum(), 0)
        self.assertTrue(ThresholdBaseline.from_dict(b.to_dict()).degenerate)

    def test_bootstrap_resamples_groups(self):
        rows = [{"experiment_id": f"e{i}", "label": i % 2, "status": "included"} for i in range(8) for _ in range(5)]
        df = pd.DataFrame(rows)
        r = E.group_bootstrap(df, np.ones(len(df)), 0.5, n_boot=50)
        self.assertEqual((r["n_groups"], r["resampled"]), (8, "experiment_id"))


if __name__ == "__main__":
    unittest.main()
