"""Link-down semantics (verified against the real kernel in test_live_kernel_lab) and cross-approach workload parity."""
import ipaddress
import json
import tempfile
import unittest
from dataclasses import replace

from app.compare import events as ev
from app.compare.experiment import ExperimentSpec
from app.compare.normalize import normalize
from app.compare.runner import run_experiment
from app.mobileip.simnet import Sim, SimLink, SimNode
from app.traffic.loader import read_csv


class SimLinkSemantics(unittest.TestCase):
    def pair(self):
        sim = Sim()
        a, b = SimNode(sim, "a"), SimNode(sim, "b")
        link = SimLink("ab", ("a", "e0"), ("b", "e0"), 1_000_000)
        a.add_iface("e0", ipaddress.IPv4Interface("10.0.0.1/24"), link)
        b.add_iface("e0", ipaddress.IPv4Interface("10.0.0.2/24"), link)
        return sim, a, b, link

    def test_both_ends_semantics_removes_address_and_route_on_both_sides(self):
        sim, a, b, link = self.pair()
        link.go_down()
        self.assertEqual((a.iface_up("e0"), b.iface_up("e0")), (False, False))
        self.assertNotIn(ipaddress.IPv4Address("10.0.0.2"), b.local_addresses())
        self.assertIsNone(b.lookup(ipaddress.IPv4Address("10.0.0.1")))

    def test_kernel_veth_semantics_keeps_the_peer_configured_but_unable_to_send(self):
        sim, a, b, link = self.pair()
        link.go_down(keep_up_ends={"b"})
        self.assertFalse(a.iface_up("e0"))
        self.assertIn(ipaddress.IPv4Address("10.0.0.2"), b.local_addresses())       # address survives, like on a real veth peer
        self.assertIsNotNone(b.lookup(ipaddress.IPv4Address("10.0.0.1")))           # connected route survives
        b.send_udp("10.0.0.2", "10.0.0.1", 1, 2, b"x")
        sim.run_until(sim.clock.mono + 10_000_000)
        self.assertEqual(sim.drop_counts.get(("b", "link_down")), 1)               # ...but nothing crosses the dead link
        link.go_up()
        self.assertTrue(a.iface_up("e0") and b.iface_up("e0") and not link.keep_up_ends)


class StaleCareOfAddressInTheEmulator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_sem(self, sem, approach="mip-basic"):
        return run_experiment(ExperimentSpec(approach, link_semantics=sem, scenario_id=f"{approach}-{sem}"), self.tmp.name)

    def drops(self, r):
        return json.loads((r.run_dir / "experiment_summary.json").read_text()).get("drops") or \
            json.loads((r.run_dir / "mip_summary.json").read_text())["drops"]

    def test_both_ends_loops_kernel_veth_does_not_but_delivery_metrics_are_identical(self):
        a, b = self.run_sem("both-ends"), self.run_sem("kernel-veth")
        da, db = self.drops(a), self.drops(b)
        self.assertGreater(da.get("ha:ttl_expired", 0), 0)                          # stale packets bounce HA <-> router
        self.assertEqual(db.get("ha:ttl_expired", 0), 0)                            # real-veth semantics: they die at the router
        self.assertGreater(db.get("ana:link_down", 0) + db.get("anb:link_down", 0), 0)
        for k in ("loss", "handoff_summary"):
            self.assertEqual(a.metrics[k], b.metrics[k], k)                         # the loop never changed any measured result

    def test_the_same_holds_for_the_tcp_baseline(self):
        a, b = self.run_sem("both-ends", "tcp-reconnect"), self.run_sem("kernel-veth", "tcp-reconnect")
        self.assertEqual(a.metrics["handoff_summary"], b.metrics["handoff_summary"])
        self.assertEqual(b.metrics["loss"]["lost"], 0)


class WorkloadParity(unittest.TestCase):
    """Are the approaches really compared on the same traffic and the same movement?"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = ExperimentSpec("tcp-reconnect", rate_pps=37, payload=200, duration_s=8, moves=((3.0, "eth-b"), (6.0, "eth-a")))
        cls.runs = {a: run_experiment(replace(base, approach=a, scenario_id=""), cls.tmp.name)
                    for a in ("tcp-reconnect", "mip-basic", "mip-mbb", "ro-model")}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rows(self, a, name):
        return read_csv(self.runs[a].run_dir / name, name).rows

    def test_every_approach_sends_the_same_probes(self):
        ref = self.rows("tcp-reconnect", "sent.csv")
        self.assertEqual(len(ref), int((8 - 0.5) * 37))
        base = ref[0]["send_mono_ns"]
        for a in self.runs:
            sent = self.rows(a, "sent.csv")
            self.assertEqual(len(sent), len(ref), a)
            self.assertEqual({r["size_bytes"] for r in sent}, {200}, a)
            self.assertEqual([r["seq"] for r in sent], list(range(len(ref))), a)
            self.assertEqual([r["send_mono_ns"] - sent[0]["send_mono_ns"] for r in sent],
                             [r["send_mono_ns"] - base for r in ref], f"{a}: send schedule differs")
            self.assertEqual({r["send_errno"] for r in sent}, {0})
            self.assertEqual([r["send_mono_ns"] for r in sent], [r["scheduled_mono_ns"] for r in sent], f"{a}: sender slip in a deterministic run")

    def test_every_approach_sees_the_same_movement_and_detection_delay(self):
        for a, r in self.runs.items():
            e = ev.read_events(r.run_dir / "approach_events.csv")
            sent0 = self.rows(a, "sent.csv")[0]["send_mono_ns"]
            moves = [(x["mono_ns"] - sent0) / 1e9 for x in e if x["kind"] == "link_change"]
            attach = [(y["mono_ns"] - x["mono_ns"]) / 1e6 for x, y in zip([x for x in e if x["kind"] == "link_change"],
                                                                         [x for x in e if x["kind"] == "attach" and x["mono_ns"] > sent0])]
            self.assertEqual([round(m + 0.5, 3) for m in moves], [3.0, 6.0], a)        # moves relative to t0 (probes start at +0.5 s)
            self.assertEqual(attach, [20.0, 20.0], a)

    def test_every_approach_counts_echo_rtt_the_same_way(self):
        for a, r in self.runs.items():
            echoes = self.rows(a, "echoes.csv")
            sent = {x["seq"]: x for x in self.rows(a, "sent.csv")}
            self.assertGreater(len(echoes), 200, a)
            self.assertTrue(all(e["recv_mono_ns"] > sent[e["seq"]]["send_mono_ns"] for e in echoes), a)
            self.assertEqual(r.metrics["rtt_ms"]["count"], len({e["seq"] for e in echoes}), a)

    def test_payload_semantics_match(self):
        for a in self.runs:
            self.assertEqual({r["size_bytes"] for r in self.rows(a, "received.csv")}, {200}, a)       # same bytes delivered to the app


class Provenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_emulated_results_say_they_are_simulated(self):
        for a in ("tcp-reconnect", "mip-basic", "mip-mbb", "ro-model"):
            p = run_experiment(ExperimentSpec(a), self.tmp.name).metrics["provenance"]
            self.assertEqual(p["data_origin"], "simulated", a)
            self.assertIn("virtual", p["clock"])
            self.assertEqual(p["analytic_values"], [])                       # the emulator COUNTS tunnel bytes
            self.assertTrue(p["timing_comparable_with_other_environments"])

    def test_loopback_results_say_they_are_real_but_not_timing_comparable(self):
        r = run_experiment(ExperimentSpec("tcp-reconnect", environment="loopback", duration_s=2.5, moves=((1.0, "eth-b"),)), self.tmp.name)
        self.assertEqual(r.status, "ok", r.reason)
        p = r.metrics["provenance"]
        self.assertEqual(p["data_origin"], "real-kernel-loopback")
        self.assertIn("real monotonic", p["clock"])
        self.assertFalse(p["timing_comparable_with_other_environments"])
        self.assertFalse(r.metrics["timing_comparable"])
        self.assertEqual(r.metrics["loss"]["lost"], 0)

    def test_analytic_overhead_is_flagged_as_analytic(self):
        r = run_experiment(ExperimentSpec("mip-basic"), self.tmp.name)
        path = r.run_dir / "experiment_summary.json"
        s = json.loads(path.read_text())
        s["overhead"]["basis"] = "analytic (RFC 2003 header; kernel tunnel bytes not observed)"       # what a live run records
        path.write_text(json.dumps(s))
        self.assertEqual(normalize(r.run_dir)["provenance"]["analytic_values"], ["mobility_overhead"])


if __name__ == "__main__":
    unittest.main()


class EventStormGuard(unittest.TestCase):
    """Found when the QUIC glue test hung forever: a timer re-armed at the same virtual instant. The simulator must fail fast."""

    def test_zero_time_event_loop_raises_instead_of_hanging(self):
        sim = Sim()
        sim.STORM_LIMIT = 1000

        def spin():
            sim.at(sim.clock.mono, spin)               # re-arm at the same instant, forever
        sim.at(sim.clock.mono, spin)
        with self.assertRaises(RuntimeError) as cm:
            sim.run_until(sim.clock.mono + 10**9)
        self.assertIn("event storm", str(cm.exception))

    def test_normal_bursts_at_one_instant_are_fine(self):
        sim = Sim()
        n = []
        for _ in range(500):
            sim.at(sim.clock.mono + 5, lambda: n.append(1))
        sim.run_until(sim.clock.mono + 10)
        self.assertEqual(len(n), 500)
