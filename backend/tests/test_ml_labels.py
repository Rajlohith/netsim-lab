import unittest

from app.ml.config import LabelConfig
from app.ml.labels import detect_episodes, find_gaps, gap_threshold_ns, label_decision_points
from tests.ml_helpers import MS, S, synthetic_run

L = LabelConfig()


class LabelTests(unittest.TestCase):
    def test_handoff_with_gap_is_an_episode_with_onset_at_handoff_start(self):
        run = synthetic_run(handoffs=((10.0, 10.1),), gap=((10.0, 10.2),))
        eps, outcomes, diag = detect_episodes(run, L)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].onset_ns, run.base + 10 * S)
        self.assertGreaterEqual(eps[0].gap_ms, 150)
        self.assertTrue(eps[0].recovered)
        self.assertEqual(outcomes[0]["outcome"], "degraded")
        self.assertAlmostEqual(diag["gap_threshold_ms"], 60.0)

    def test_handoff_without_gap_is_benign_not_positive(self):
        run = synthetic_run(handoffs=((10.0, 10.1),), gap=())
        eps, outcomes, _ = detect_episodes(run, L)
        self.assertEqual((eps, outcomes[0]["outcome"]), ([], "benign"))

    def test_gap_below_threshold_is_ignored(self):
        run = synthetic_run(handoffs=((10.0, 10.1),), gap=((10.0, 10.03),))      # 3 missing probes: arrival gap ~40 ms < 60 ms
        self.assertEqual(detect_episodes(run, L)[0], [])

    def test_gap_without_handoff_is_never_labelled(self):
        run = synthetic_run(handoffs=(), gap=((10.0, 10.5),))
        eps, outcomes, diag = detect_episodes(run, L)
        self.assertEqual((eps, outcomes), ([], []))
        self.assertEqual(len(diag["unrelated_gaps"]), 1)

    def test_unrecovered_outage_runs_to_the_end(self):
        run = synthetic_run(duration_s=20, handoffs=((10.0, None),), gap=((10.0, 99.0),))
        eps, _, _ = detect_episodes(run, L)
        self.assertEqual(len(eps), 1)
        self.assertFalse(eps[0].recovered)
        self.assertEqual(eps[0].end_ns, run.base + 20 * S)

    def test_second_handoff_inside_same_gap_is_merged(self):
        run = synthetic_run(handoffs=((10.0, 10.1), (10.3, 10.4)), gap=((10.0, 10.6),))
        eps, outcomes, _ = detect_episodes(run, L)
        self.assertEqual(len(eps), 1)
        self.assertEqual([o["outcome"] for o in outcomes], ["degraded", "merged"])

    def test_threshold_scales_with_probe_interval(self):
        slow = synthetic_run(interval_ms=50)
        self.assertAlmostEqual(gap_threshold_ns(slow, L) / 1e6, 150.0)

    def test_label_window_boundaries_and_exclusions(self):
        run = synthetic_run(handoffs=((10.0, 10.1),), gap=((10.0, 10.2),))
        eps, _, _ = detect_episodes(run, L)
        onset = eps[0].onset_ns
        ts = [onset - int(1.5 * S), onset - int(1.0 * S), onset - int(0.999 * S), onset - 1, onset, onset + int(0.05 * S), eps[0].end_ns + 1,
              run.base + int(19.5 * S)]
        out = label_decision_points(ts, eps, run.observed_end_ns, L)
        got = [(o["label"], o["status"]) for o in out]
        self.assertEqual(got, [(0, "included"),                      # 1.5 s before: outside the 1 s horizon
                               (1, "included"),                      # onset - t == horizon: inside (t, t+H]
                               (1, "included"), (1, "included"),
                               (0, "excluded_in_episode"),           # t == onset: already degrading
                               (0, "excluded_in_episode"),
                               (0, "included"),                      # after the episode
                               (0, "excluded_censored_horizon")])    # t + 1 s beyond the end of the run
        self.assertAlmostEqual(out[1]["time_to_onset_s"], 1.0)

    def test_labels_are_reproducible(self):
        a = detect_episodes(synthetic_run(), L)[0]
        b = detect_episodes(synthetic_run(), L)[0]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
