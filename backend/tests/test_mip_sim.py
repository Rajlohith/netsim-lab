"""End-to-end Mobile IP behaviour on the emulated network: real protocol code, real IPv4/IP-in-IP bytes."""
import json
import tempfile
import unittest
from pathlib import Path

from app.mobileip.analyze import analyze_mip_run
from app.mobileip.events import read_events
from app.mobileip.mobility_sim import HOME_ADDR, MobilitySim, MobilitySpec
from app.traffic.loader import read_csv
from app.traffic.schema import validate_meta


class SimBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_sim(self, spec=None, customise=None, **kw):
        spec = spec or MobilitySpec(**kw)
        sim = MobilitySim(spec, self.tmp.name)
        if customise:
            customise(sim)
        run_dir = sim.run()
        return sim, run_dir, analyze_mip_run(run_dir)


class HandoffTests(SimBase):
    def setUp(self):
        super().setUp()
        self.sim, self.run_dir, self.m = self.run_sim()

    def test_binding_updates_change_the_reachable_location(self):
        via = [t["reachable_via"] for t in self.m["binding_timeline"]]
        self.assertEqual(via, ["10.10.4.2", "10.10.5.2", "10.10.4.2", None])
        evs = [t["event"] for t in self.m["binding_timeline"]]
        self.assertEqual(evs, ["binding_created", "binding_updated", "binding_updated", "binding_removed"])
        # the data really follows: probes see the A path (40 ms RTT), then B (80 ms), then A again
        h1, h2 = self.m["connectivity"]["handoffs"]
        self.assertEqual((h1["rtt_median_before_ms"], h1["rtt_median_after_ms"]), (40.0, 80.0))
        self.assertEqual((h2["rtt_median_before_ms"], h2["rtt_median_after_ms"]), (80.0, 40.0))

    def test_first_tunnelled_packet_per_care_of_address(self):
        ev, _ = read_events(self.run_dir)
        encap = [e["care_of_address"] for e in ev if e["kind"] == "tunnel_encap"]
        decap = [e["care_of_address"] for e in ev if e["kind"] == "tunnel_decap"]
        self.assertEqual(encap, ["10.10.4.2", "10.10.5.2"])         # the A->A return needs no new "first" event
        self.assertEqual(decap, ["10.10.4.2", "10.10.5.2"])

    def test_packets_reach_the_mobile_node_after_registration(self):
        rx = read_csv(self.run_dir / "received.csv", "received.csv").rows
        sent = read_csv(self.run_dir / "sent.csv", "sent.csv").rows
        self.assertGreater(len(rx), 900)
        self.assertTrue(all(r["src_addr"] == "10.10.1.2" for r in rx))
        first_registered = self.m["episodes"][0]["registered_mono_ns"]
        self.assertGreater(min(r["recv_mono_ns"] for r in rx), first_registered)
        loss = self.m["connectivity"]["loss"]
        self.assertTrue(loss["exact"])
        # all loss is inside the two handoff bursts
        bursts = sum(h["loss_burst_packets"] for h in self.m["connectivity"]["handoffs"])
        self.assertEqual(loss["lost"], bursts)
        self.assertEqual(len(sent) - len(rx), loss["lost"])

    def test_registration_delay_matches_path_delay(self):
        # request + reply over A: 2 x (10 + 5) ms + 1 ms HA processing; over B: 2 x (20 + 15) + 1
        delays = [r["delay_ms"] for r in self.m["registrations"] if r["requested_lifetime_s"] != 0]
        self.assertEqual(delays, [31.0, 71.0, 31.0])
        self.assertEqual(self.m["registration_delay_ms"]["count"], 3)

    def test_handoff_events_and_latency_recorded(self):
        eps = self.m["episodes"]
        self.assertEqual([e["outcome"] for e in eps], ["registered"] * 3)
        self.assertEqual([e["detection_ms"] for e in eps], [None, 20.0, 20.0])
        self.assertEqual([e["handoff_latency_ms"] for e in eps], [None, 91.0, 51.0])
        ev = read_csv(self.run_dir / "events.csv", "events.csv").rows
        self.assertEqual([(e["event_id"], e["kind"]) for e in ev],
                         [("scenario", "scenario_start"), ("handoff-1", "handoff_start"), ("handoff-1", "handoff_end"),
                          ("handoff-2", "handoff_start"), ("handoff-2", "handoff_end"), ("scenario", "scenario_end")])
        for h in self.m["connectivity"]["handoffs"]:
            self.assertTrue(h["recovered"])
            self.assertTrue(h["interruption_ms"] > h["handoff_latency_ms"] - 1)   # outage covers detect + register

    def test_tunnel_overhead_is_measured_from_real_encapsulation(self):
        t = self.m["tunnel"]
        self.assertTrue(t["measured"])
        self.assertEqual(t["overhead_bytes_per_packet"], 20.0)
        self.assertAlmostEqual(t["overhead_percent_of_inner"], 100 * 20 / 92, places=4)   # inner = 20 IP + 8 UDP + 64
        self.assertEqual(t["inner_bytes"], 92 * t["packets"])
        self.assertEqual(t["outer_bytes"], 112 * t["packets"])
        self.assertEqual(t["packets"], self.sim.ha.stats["tunnel_packets"])

    def test_stale_care_of_address_packets_loop_until_ttl(self):
        # emulator default ('both-ends' link semantics): packets for the old CoA bounce HA <-> access router. On a real kernel this only
        # happens when BOTH ends of the link are down (see test_live_kernel_lab); see docs/MOBILE_IP.md section 3
        drops = json.loads((self.run_dir / "mip_summary.json").read_text())["drops"]
        self.assertGreater(drops.get("ha:ttl_expired", 0), 0)
        self.assertEqual(self.m["failures"]["packet_drops_by_reason"], drops)

    def test_cleanup_leaves_no_state(self):
        self.assertEqual(len(self.sim.ha.bindings), 0)
        self.assertEqual(self.sim.ha_dp.tunnels, {})
        self.assertEqual((self.sim.mn.state, self.sim.mn_dp.care_of), ("detached", None))
        ev, _ = read_events(self.run_dir)
        self.assertEqual([e["detail"] for e in ev if e["kind"] == "binding_removed"], ["deregistered"])
        self.assertIn("tunnel_stats", [e["kind"] for e in ev])

    def test_output_files_follow_the_schemas(self):
        for role in ("sender", "receiver"):
            meta = json.loads((self.run_dir / f"{role}.meta.json").read_text())
            self.assertEqual(validate_meta(meta), [])
            self.assertEqual((meta["scenario_id"], meta["run_id"]), ("mip-handoff", "sim-1"))
            self.assertIn("SIMULATED", meta["notes"][0])
        self.assertTrue((self.run_dir / "mip_metrics.json").exists())
        self.assertTrue((self.run_dir / "metrics.json").exists())

    def test_runs_are_exactly_repeatable(self):
        with tempfile.TemporaryDirectory() as other:
            spec = MobilitySpec(run_id="sim-2")
            run2 = MobilitySim(spec, other).run()
            for name in ("mip_events.csv", "sent.csv", "received.csv", "echoes.csv", "events.csv"):
                self.assertEqual((self.run_dir / name).read_bytes(), (run2 / name).read_bytes(), name)


class ReachabilityTests(SimBase):
    def test_no_delivery_before_registration(self):
        sim, run_dir, m = self.run_sim(probe_start_s=0.0, moves=(), deregister_at_end=False, duration_s=2.0)
        lost_seqs = m["connectivity"]["loss"]
        self.assertGreater(lost_seqs["lost"], 0)
        first_rx = read_csv(run_dir / "received.csv", "received.csv").rows[0]
        self.assertGreater(first_rx["seq"], 0)                             # early probes were dropped at the HA
        self.assertEqual(sim.sim.drop_counts[("ha", "no_binding")], lost_seqs["lost"])
        self.assertEqual(m["failures"]["tunnel_drops_first_seen"], ["no_binding"])

    def test_expiry_without_renewal_cuts_connectivity_and_removes_tunnel(self):
        def no_renewal(sim):
            sim.mn.rereg_fraction = 100.0
        sim, run_dir, m = self.run_sim(customise=no_renewal, lifetime_s=2, moves=(), deregister_at_end=False,
                                       duration_s=6.0)
        self.assertEqual(m["failures"]["binding_expired"], 1)
        self.assertEqual(sim.ha_dp.tunnels, {})
        self.assertEqual([t["event"] for t in m["binding_timeline"]], ["binding_created", "binding_expired"])
        loss = m["connectivity"]["loss"]
        self.assertGreater(loss["lost"], 300)                              # ~4 s of 100 pps after expiry
        self.assertEqual(sim.sim.drop_counts[("ha", "no_binding")], loss["lost"])

    def test_periodic_reregistration_keeps_connectivity(self):
        sim, run_dir, m = self.run_sim(lifetime_s=2, moves=(), deregister_at_end=False, duration_s=6.0)
        self.assertEqual(m["connectivity"]["loss"]["lost"], 0)
        self.assertGreaterEqual(len(m["registrations"]), 5)
        self.assertEqual(m["failures"]["binding_expired"], 0)
        self.assertEqual([t["event"] for t in m["binding_timeline"]][0], "binding_created")


class FailureTests(SimBase):
    def test_wrong_key_means_no_registration_and_no_delivery(self):
        sim, run_dir, m = self.run_sim(mn_key=b"x" * 32, moves=(), deregister_at_end=False, duration_s=3.0)
        self.assertGreaterEqual(m["failures"]["auth_failure"], 1)
        self.assertGreaterEqual(m["failures"]["reply_auth_invalid"], 1)
        self.assertEqual(m["binding_timeline"], [])
        self.assertEqual(read_csv(run_dir / "received.csv", "received.csv").rows, [])
        self.assertEqual(m["connectivity"]["loss"]["lost"], m["connectivity"]["loss"]["sent_ok"])
        self.assertEqual(m["episodes"][0]["outcome"], "incomplete")        # MN was still retrying, never accepted

    def test_unreachable_home_agent_retransmits_then_recovers(self):
        def cut_ha(sim):
            for n in ("ha-ana", "ha-anb"):
                sim.sim.links[n].up = False
            sim.mn.rt_initial = int(0.1e9)
            sim.sim.at(sim.sim.clock.mono + int(0.35e9), lambda: setattr(sim.sim.links["ha-ana"], "up", True))
        sim, run_dir, m = self.run_sim(customise=cut_ha, moves=(), deregister_at_end=False, duration_s=3.0,
                                       probe_start_s=1.0)
        ep = m["episodes"][0]
        self.assertEqual(ep["outcome"], "registered")
        self.assertGreaterEqual(ep["retransmits"], 2)
        self.assertGreater(ep["time_to_registered_ms"], 350)
        self.assertEqual(m["retransmissions"], ep["retransmits"])
        self.assertEqual(m["connectivity"]["loss"]["lost"], 0)              # probes only started after recovery

    def test_permanent_outage_ends_in_recorded_timeout(self):
        def cut_ha(sim):
            sim.sim.links["ha-ana"].up = False
            sim.mn.rt_initial = int(0.1e9)
        sim, run_dir, m = self.run_sim(customise=cut_ha, moves=(), deregister_at_end=False, duration_s=3.0)
        self.assertEqual((m["failures"]["reg_timeout"], m["failures"]["reg_failed"]), (1, 1))
        self.assertEqual(m["episodes"][0]["outcome"], "failed")
        self.assertEqual(sim.mn.state, "failed")
        self.assertEqual(m["registration_delay_ms"]["count"], 0)
        self.assertIsNone(m["registration_delay_ms"]["mean"])                # no replies -> no delay numbers invented

    def test_clock_skewed_mobile_node_recovers_and_is_recorded(self):
        sim, run_dir, m = self.run_sim(mn_clock_skew_s=3600.0, moves=(), deregister_at_end=False, duration_s=3.0)
        ep = m["episodes"][0]
        self.assertEqual((ep["outcome"], ep["rejected_133"]), ("registered", 1))
        self.assertEqual(m["failures"]["replay_rejected"], 1)
        self.assertAlmostEqual(ep["time_to_registered_ms"], 62.0, delta=0.5)     # two request/reply rounds
        self.assertEqual(m["connectivity"]["loss"]["lost"], 0)


if __name__ == "__main__":
    unittest.main()
