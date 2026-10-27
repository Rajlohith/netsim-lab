"""Controlled end-to-end scenarios over real UDP sockets on loopback (impairment proxy, no root)."""
import random
import tempfile
import unittest

from app.traffic.loopback_proxy import PathRules
from tests.traffic_helpers import run_loopback


class LoopbackScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_clean_path_has_no_loss(self):
        m, _, st = run_loopback(self.tmp.name, PathRules(), count=200, rate=400)
        self.assertEqual((m["loss"]["lost"], m["loss"]["sent_ok"], m["loss"]["exact"]), (0, 200, True))
        self.assertEqual(m["run_status"], "complete")
        self.assertEqual(m["rtt_ms"]["count"], 200)

    def test_loss_is_computed_from_sequence_numbers(self):
        drops = {5, 17, 18, 19, 40}
        m, _, st = run_loopback(self.tmp.name, PathRules(drop_seqs=drops), count=100, rate=200)
        L = m["loss"]
        self.assertEqual(L["lost_seqs"], sorted(drops))
        self.assertEqual((L["sent_ok"], L["delivered"], L["lost"], L["loss_percent"]), (100, 95, 5, 5.0))
        self.assertEqual(L["longest_burst_packets"], 3)
        self.assertEqual(st["fwd_dropped"], 5)                     # ground truth from the proxy
        self.assertEqual(m["rtt_ms"]["count"], 95)                 # no echo for undelivered probes
        self.assertTrue(L["exact"])

    def test_seeded_random_loss_matches_generator(self):
        rng = random.Random(7)
        expected = {i for i in range(300) if rng.random() < 0.2}
        m, _, st = run_loopback(self.tmp.name, PathRules(drop_prob=0.2, seed=7), count=300, rate=500)
        self.assertEqual(set(m["loss"]["lost_seqs"]), expected)
        self.assertEqual(m["loss"]["lost"], len(expected))

    def test_configured_delay_is_measured(self):
        m, _, _ = run_loopback(self.tmp.name, PathRules(delay_ms=20), count=100, rate=100)
        rtt, owd = m["rtt_ms"], m["owd_ms"]
        self.assertGreaterEqual(rtt["min"], 40.0)                   # physically cannot be lower
        self.assertLess(rtt["mean"], 40.0 + 15.0)                   # scheduling slack
        self.assertGreaterEqual(owd["min"], 20.0)
        self.assertLess(owd["mean"], 20.0 + 10.0)
        self.assertLess(m["jitter"]["rfc3550_final_ms"], 10.0)
        self.assertEqual(m["loss"]["lost"], 0)

    def test_throughput_matches_offered_rate(self):
        m, _, _ = run_loopback(self.tmp.name, PathRules(), duration=1.0, rate=100, size=200)
        # 100 pps * 200 B * 8 = 160 kbit/s
        self.assertAlmostEqual(m["throughput"]["offered_bps"], 160_000, delta=8_000)
        self.assertAlmostEqual(m["throughput"]["goodput_bps"], 160_000, delta=8_000)

    def test_handoff_blackout_under_continuous_traffic(self):
        rules = PathRules(delay_schedule=[(0, 10), (1.4, 30)], blackouts=[(1.0, 1.4)])
        m, run_dir, st = run_loopback(self.tmp.name, rules, duration=3.0, rate=100)
        h = m["handoffs"][0]
        self.assertEqual(h["event_id"], "handoff-1")
        burst = h["loss_burst"]
        self.assertTrue(30 <= burst["packets"] <= 50, burst)        # ~400 ms / 10 ms
        self.assertTrue(h["recovered"])
        self.assertTrue(380 <= h["interruption_ms"] <= 500, h["interruption_ms"])
        self.assertEqual(m["loss"]["lost"], burst["packets"])        # nothing lost outside the handoff
        self.assertTrue(20.0 <= h["rtt_median_before_ms"] < 35.0, h)  # 2 x 10 ms
        self.assertTrue(60.0 <= h["rtt_median_after_ms"] < 80.0, h)   # 2 x 30 ms (new path)
        self.assertTrue((run_dir / "events.csv").exists())
        self.assertTrue((run_dir / "metrics.json").exists())

    def test_results_carry_scenario_identifiers_and_timestamps(self):
        import json
        m, run_dir, _ = run_loopback(self.tmp.name, PathRules(), count=20, scenario="my-scn", run="run-42")
        self.assertEqual((m["scenario_id"], m["run_id"]), ("my-scn", "run-42"))
        for role in ("sender", "receiver"):
            meta = json.loads((run_dir / f"{role}.meta.json").read_text())
            self.assertEqual((meta["scenario_id"], meta["run_id"]), ("my-scn", "run-42"))
            self.assertTrue(meta["start_wall_iso"].endswith("Z") and meta["end_wall_iso"].endswith("Z"))
            self.assertLess(meta["start_mono_ns"], meta["end_mono_ns"])
            self.assertEqual(meta["status"], "completed")
        self.assertIn("generated_wall_iso", m)


if __name__ == "__main__":
    unittest.main()
