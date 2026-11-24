"""Sanity checks for the ML handoff-anticipation study (synthetic signal; see app/compare/ml_handoff.py)."""
import unittest

import numpy as np

from app.compare import ml_handoff as H


class MlHandoff(unittest.TestCase):
    def test_reactive_blackout_matches_rate_times_blackout(self):
        leads = np.zeros(5)
        out = H.summarise("reactive", leads, 0, 1.0, 5)
        self.assertAlmostEqual(out["mip-basic"]["mean_lost_packets_per_handoff"], H.RATE_PPS * H.SIM["mip-basic"]["blackout_ms"] / 1000.0)

    def test_lead_longer_than_blackout_means_zero_loss(self):
        leads = np.full(4, 0.3)
        out = H.summarise("early", leads, 0, 1.0, 0)
        self.assertEqual(out["mip-basic"]["mean_lost_packets_per_handoff"], 0.0)
        self.assertEqual(out["mip-basic"]["share_of_handoffs_with_zero_loss"], 1.0)

    def test_trajectories_are_reproducible_and_labelled(self):
        a, b = H.build(6, 5), H.build(6, 5)
        self.assertTrue(all(np.array_equal(x["X"], y["X"]) and x["event"] == y["event"] for x, y in zip(a, b)))
        for t in a:
            y = H.labels(t)
            self.assertEqual(int(y.sum()) > 0, t["event"] is not None)


if __name__ == "__main__":
    unittest.main()
