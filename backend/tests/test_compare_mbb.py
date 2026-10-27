"""Make-before-break on the Mobile IP testbed (emulator) and the regression for the premature handoff_end bug."""
import tempfile
import unittest
from dataclasses import replace

from app.compare.experiment import DEFAULT_TOPOLOGY, ExperimentSpec
from app.compare.normalize import normalize
from app.compare.runner import run_experiment
from app.mobileip.events import read_events
from app.mobileip.mobility_sim import MobilitySim, MobilitySpec
from app.traffic.analyze import analyze_run


class MbbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_mip(self, **kw):
        spec = MobilitySpec(topology=DEFAULT_TOPOLOGY, duration_s=8, moves=((3.0, "eth-b"), (6.0, "eth-a")), **kw)
        sim = MobilitySim(spec, self.tmp.name)
        run = sim.run()
        return sim, run, analyze_run(run)

    def test_handoff_end_is_recorded_when_registration_completes(self):
        """Regression: the mobile node still says 'registered' (old binding) until it re-attaches."""
        _, run, m = self.run_mip(run_id="bbm")
        durations = [h["action_duration_ms"] for h in m["handoffs"]]
        self.assertEqual(durations, [91.0, 51.0])        # detection 20 ms + registration round trip (71 / 31 ms)

    def test_without_overlap_every_handoff_loses_packets(self):
        _, _, m = self.run_mip(run_id="bbm")
        self.assertEqual(m["loss"]["lost"], 16)

    def test_overlap_with_drain_time_loses_nothing(self):
        sim, run, m = self.run_mip(run_id="mbb", make_before_break=True, old_link_hold_ms=100)
        self.assertEqual((m["loss"]["lost"], m["loss"]["exact"]), (0, True))
        self.assertEqual(m["reordered_packets"], 1)     # B->A: packets on the shorter new path overtake the old path's
        self.assertNotIn(("mn", "tunnel_unexpected"), sim.sim.drop_counts)

    def test_overlap_without_drain_time_still_loses_in_flight_packets(self):
        _, _, m = self.run_mip(run_id="mbb0", make_before_break=True, old_link_hold_ms=0)
        self.assertEqual(m["loss"]["lost"], 2)          # releasing the old link at registration drops packets still on it
        self.assertGreater(m["loss"]["lost"], 0)

    def test_old_link_is_released_exactly_hold_time_after_registration(self):
        sim, run, m = self.run_mip(run_id="mbb1", make_before_break=True, old_link_hold_ms=100, deregister_at_end=False)
        rows, _ = read_events(run)
        replies = [r for r in rows if r["kind"] == "reg_reply_received" and r["node"] == "mn" and r["care_of_address"] == "10.10.5.2"]
        detach = [r for r in rows if r["kind"] == "detach" and "old link" in r["detail"] and r["care_of_address"] == "10.10.4.2"]
        self.assertEqual(len(detach), 1)
        self.assertEqual(detach[0]["mono_ns"] - replies[0]["mono_ns"], 100_000_000)
        self.assertEqual(sim.mn_dp.accept, {sim.mn_dp.care_of})   # only the current care-of address once the overlap is over

    def test_registration_leaves_through_the_new_interface(self):
        sim, run, m = self.run_mip(run_id="mbb2", make_before_break=True)
        rows, _ = read_events(run)
        recv = [r["detail"] for r in rows if r["kind"] == "reg_request_received"]
        self.assertTrue(any("from 10.10.5.2" in d for d in recv), recv)      # source = new CoA, while the old link was still up

    def test_mbb_shrinks_the_service_gap_but_not_the_registration_time(self):
        base = ExperimentSpec("mip-basic")
        a = run_experiment(replace(base, approach="mip-basic", scenario_id="a"), self.tmp.name)
        b = run_experiment(replace(base, approach="mip-mbb", scenario_id="b"), self.tmp.name)
        self.assertEqual(a.metrics["handoff_summary"]["handoff_latency_ms_mean"], b.metrics["handoff_summary"]["handoff_latency_ms_mean"])
        self.assertGreaterEqual(a.metrics["handoff_summary"]["service_gap_ms_max"], 100)
        self.assertLessEqual(b.metrics["handoff_summary"]["service_gap_ms_max"], 40)
        self.assertEqual((a.metrics["loss"]["lost"], b.metrics["loss"]["lost"]), (16, 0))

    def test_moving_back_before_the_old_link_release_fires_does_not_kill_the_new_link(self):
        """Regression: handoff 1 scheduled 'release eth-a' (100 ms after registration); the node moved back to eth-a at +150 ms
        and the stale timer then took the NEW link down (registration lost, 381 of 550 packets lost)."""
        spec = MobilitySpec(topology=DEFAULT_TOPOLOGY, duration_s=6, moves=((2.0, "eth-b"), (2.15, "eth-a")),
                            make_before_break=True, old_link_hold_ms=100, deregister_at_end=False, run_id="rapid")
        sim = MobilitySim(spec, self.tmp.name)
        m = analyze_run(sim.run())
        self.assertEqual(sim.mn.state, "registered")
        self.assertEqual(m["loss"]["lost"], 0)
        self.assertTrue(sim.mn_node.ifaces["eth-a"][1].up)
        rows, _ = read_events(sim.run_dir)
        bound = [r["care_of_address"] for r in rows if r["kind"] in ("binding_created", "binding_updated")]
        self.assertEqual(bound, ["10.10.4.2", "10.10.5.2", "10.10.4.2"])         # the binding followed both moves

    def test_other_approaches_refuse_mbb_with_a_reason(self):
        for approach in ("tcp-reconnect", "mip-basic", "ro-model", "quic-migration"):
            r = run_experiment(ExperimentSpec(approach, handoff_mode="mbb", scenario_id=f"u-{approach}"), self.tmp.name)
            self.assertEqual(r.status, "unsupported", approach)
            self.assertIn("make-before-break", r.reason)
            self.assertIsNone(r.metrics)


if __name__ == "__main__":
    unittest.main()
