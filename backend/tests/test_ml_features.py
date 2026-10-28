import math
import unittest

import numpy as np

from app.ml.config import FeatureConfig
from app.ml.features import (EVENT_CAPABILITY, FEATURE_NAMES, UNAVAILABLE_FEATURES, LeakageError, extract_features, feature_availability)
from tests.ml_helpers import MS, S, synthetic_run

CFG = FeatureConfig()


class FeatureTests(unittest.TestCase):
    def test_steady_state_values(self):
        run = synthetic_run()
        f = extract_features(run, run.base + 5 * S, CFG).values
        self.assertAlmostEqual(f["rtt_last_ms"], 30.0)
        self.assertAlmostEqual(f["rtt_mean_short_ms"], 30.0)
        self.assertAlmostEqual(f["rtt_trend_ms"], 0.0)
        self.assertAlmostEqual(f["rtt_slope_ms_per_s"], 0.0, places=6)
        self.assertAlmostEqual(f["jitter_ms"], 0.0)
        self.assertEqual(f["loss_rate_short"], 0.0)
        self.assertAlmostEqual(f["goodput_short_kbps"], 64 * 8 * 100 / 1000, places=1)     # 100 probes/s x 64 B
        self.assertLess(f["echo_overdue_ratio"], 1.0)
        self.assertTrue(math.isnan(f["time_since_last_handoff_start_s"]))
        self.assertEqual(f["handoffs_started_total"], 0.0)

    def test_loss_counts_only_probes_older_than_grace(self):
        run = synthetic_run(gap=((10.0, 10.2),))
        t = run.base + int(10.5 * S)
        f = extract_features(run, t, FeatureConfig(short_window_s=1.0, loss_grace_ms=250.0)).values
        # probes sent in (9.25, 10.25] s; those whose echo (send+30ms) would land in [10.0,10.2): sent 9.97..10.17 -> 20 of 100
        self.assertAlmostEqual(f["loss_rate_short"], 0.20, delta=0.03)
        # a probe sent 100 ms ago is 'pending', never counted as lost
        g = extract_features(run, run.base + int(10.1 * S), FeatureConfig(loss_grace_ms=250.0)).values
        self.assertEqual(g["loss_rate_short"], 0.0)

    def test_gap_shows_in_overdue_ratio(self):
        run = synthetic_run()
        f = extract_features(run, run.base + int(10.15 * S), CFG).values
        self.assertGreater(f["echo_overdue_ratio"], 10.0)
        self.assertGreater(f["time_since_last_echo_ms"], 100.0)

    def test_rtt_slope_detects_ramp(self):
        run = synthetic_run(gap=())
        # rewrite RTT as a ramp: later probes slower
        run.echo_rtt_ms = 20.0 + run.echo_recv_ns / 1.0 * 0 + (run.echo_recv_ns - run.base) / S * 2.0
        f = extract_features(run, run.base + 8 * S, CFG).values
        self.assertAlmostEqual(f["rtt_slope_ms_per_s"], 2.0, places=3)
        self.assertGreater(f["rtt_trend_ms"], 0)

    def test_history_features_use_only_started_and_ended_events(self):
        run = synthetic_run(handoffs=((4.0, 4.1), (10.0, 10.1)))
        f = extract_features(run, run.base + int(4.05 * S), CFG).values
        self.assertEqual((f["handoffs_started_total"], f["handoff_in_progress"]), (1.0, 1.0))
        self.assertTrue(math.isnan(f["time_since_last_handoff_end_s"]))      # the end is in the future of t
        g = extract_features(run, run.base + int(6.0 * S), CFG).values
        self.assertEqual(g["handoff_in_progress"], 0.0)
        self.assertAlmostEqual(g["time_since_last_handoff_start_s"], 2.0)
        self.assertAlmostEqual(g["time_since_last_handoff_end_s"], 1.9, places=6)
        self.assertAlmostEqual(g["last_handoff_duration_ms"], 100.0, places=3)
        self.assertEqual(g["handoffs_started_total"], 1.0)                   # the handoff at 10 s is invisible at 6 s

    def test_no_future_information_by_truncation(self):
        """Features at t must be identical on the full run and on a copy physically cut at t."""
        run = synthetic_run(handoffs=((4.0, 4.1), (10.0, 10.1)), gap=((4.0, 4.1), (10.0, 10.2)),
                            events=[(3.0, "retransmit"), (4.05, "failure"), (12.0, "failure")])
        for sec in (2.0, 3.5, 4.05, 4.3, 9.99, 10.0, 10.1, 10.5, 12.5, 15.0):
            t = run.base + int(sec * S)
            a, b = extract_features(run, t, CFG), extract_features(run.truncated(t), t, CFG)
            for k in FEATURE_NAMES:
                x, y = a.values[k], b.values[k]
                self.assertTrue(x == y or (math.isnan(x) and math.isnan(y)), (sec, k, x, y))
            self.assertLessEqual(a.last_observation_ns, t)

    def test_changing_the_future_does_not_change_features(self):
        run = synthetic_run(gap=())
        t = run.base + 6 * S
        before = extract_features(run, t, CFG).values
        mutated = synthetic_run(gap=((6.5, 9.0),), handoffs=((6.5, 9.0),))     # different future, identical past
        after = extract_features(mutated, t, CFG).values
        for k in FEATURE_NAMES:
            self.assertTrue(before[k] == after[k] or (math.isnan(before[k]) and math.isnan(after[k])), k)

    def test_leakage_guard_raises(self):
        run = synthetic_run()
        run.echo_recv_ns = run.echo_recv_ns.copy()
        # corrupt the arrays so that an "arrived" echo is later than t: searchsorted cannot see it, so craft via last_observation check
        from app.ml import features as F
        orig = F.np.searchsorted
        try:
            F.np.searchsorted = lambda a, v, side="left": orig(a, v + 10 * S, side)     # simulate a window bug that reaches into the future
            with self.assertRaises(LeakageError):
                extract_features(run, run.base + 5 * S, CFG)
        finally:
            F.np.searchsorted = orig

    def test_unavailable_features_are_nan_never_zero(self):
        tcp = synthetic_run(approach="tcp-reconnect", events=[(3.0, "retransmit")])
        f = extract_features(tcp, tcp.base + 5 * S, CFG).values
        self.assertTrue(math.isnan(f["retransmit_count_long"]))              # TcpLite does not log retransmissions
        self.assertEqual(f["timeout_failure_count_long"], 0.0)
        noev = synthetic_run(approach="mip-basic")
        noev.approach_events_present = False
        g = extract_features(noev, noev.base + 5 * S, CFG).values
        self.assertTrue(math.isnan(g["retransmit_count_long"]) and math.isnan(g["timeout_failure_count_long"]))
        mip = synthetic_run(approach="mip-basic", events=[(3.0, "retransmit"), (4.0, "failure")])
        h = extract_features(mip, mip.base + 5 * S, CFG).values
        self.assertEqual((h["retransmit_count_long"], h["timeout_failure_count_long"]), (1.0, 1.0))

    def test_availability_table_and_unavailable_list(self):
        av = feature_availability("tcp-reconnect", True)
        self.assertFalse(av["retransmit_count_long"][0])
        self.assertTrue(av["rtt_last_ms"][0])
        self.assertEqual(set(EVENT_CAPABILITY), {"retransmit_count_long", "timeout_failure_count_long"})
        names = {n for n, _ in UNAVAILABLE_FEATURES}
        self.assertIn("signal_strength_dbm", names)
        self.assertFalse(names & set(FEATURE_NAMES))                          # nothing listed as unavailable is also a feature


if __name__ == "__main__":
    unittest.main()
