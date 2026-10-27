"""The TCP baseline in the emulator: continuity, detection policies, and expected failure cases."""
import tempfile
import unittest
from pathlib import Path

from app.compare.experiment import ExperimentSpec
from app.compare.normalize import normalize
from app.compare.transport_lab import TcpLab
from app.traffic.loader import read_csv


class TcpBaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_lab(self, customise=None, **kw):
        spec = ExperimentSpec("tcp-reconnect", **kw)
        lab = TcpLab(spec, self.tmp.name)
        if customise:
            lab.after_build.append(customise)
        run = lab.run()
        return lab, normalize(run), run

    def test_session_continues_without_loss_over_new_connections(self):
        lab, m, run = self.run_lab()
        self.assertEqual((m["loss"]["lost"], m["duplicates"], m["reordered_packets"]), (0, 0, 0))
        self.assertTrue(m["loss"]["exact"])
        self.assertEqual(lab.client.connections_opened, 3)
        self.assertFalse(m["continuity"]["transport_connection_preserved"])
        self.assertFalse(m["continuity"]["endpoint_address_preserved"])
        self.assertTrue(m["continuity"]["all_handoffs_recovered"])
        rx = read_csv(run / "received.csv", "received.csv").rows
        self.assertEqual([r["seq"] for r in rx], list(range(len(rx))))        # contiguous and in order
        self.assertEqual(len(rx), 950)

    def test_handoff_latency_is_detection_plus_two_round_trips(self):
        lab, m, _ = self.run_lab()
        h1, h2 = m["handoffs"]
        # A -> B: 20 ms detection + SYN/SYN-ACK/HELLO/DATA over the direct path cn-anb-mn (12+15 = 27 ms one way) x 4 hops-of-time
        self.assertEqual(h1["handoff_latency_ms"], 128.0)
        self.assertEqual(h2["handoff_latency_ms"], 72.0)                       # B -> A, direct path 13 ms one way
        self.assertEqual((h1["rtt_before_ms"], h1["rtt_after_ms"]), (26.0, 54.0))
        self.assertEqual(h1["signalling_messages"], 4)                         # SYN, SYN-ACK, ACK, HELLO

    def test_stall_timeout_adds_the_detection_timeout(self):
        _, ev, _ = self.run_lab(tcp_detect="event", run_id="e")
        _, to, _ = self.run_lab(tcp_detect="timeout", tcp_detect_timeout_s=0.5, run_id="t")
        d = to["handoff_summary"]["handoff_latency_ms_mean"] - ev["handoff_summary"]["handoff_latency_ms_mean"]
        self.assertTrue(400 <= d <= 560, d)                                    # ~ the 500 ms stall timeout (watchdog granularity)
        self.assertEqual(to["loss"]["lost"], 0)

    def test_server_that_stops_accepting_means_no_recovery(self):
        """Expected failure: the baseline cannot recover if the peer refuses new connections."""
        def refuse(lab):
            lab.sim.at(lab.t0 + int(3.5e9), lambda: lab.cn_stack.accept_cb.clear())   # listener disappears before the move
        lab, m, run = self.run_lab(customise=refuse, moves=((4.0, "eth-b"),))
        self.assertFalse(m["handoffs"][0]["recovered"])
        self.assertIsNone(m["handoffs"][0]["handoff_latency_ms"])
        self.assertFalse(m["continuity"]["all_handoffs_recovered"])
        self.assertGreater(m["loss"]["lost"], 0)                               # data after the move never arrives
        self.assertGreater(lab.cn_stack.stats["resets_sent"], 0)
        self.assertGreater(m["handoffs"][0]["signalling_messages"], 4)         # it keeps retrying

    def test_old_connection_cannot_leak_through_the_new_interface(self):
        lab, m, _ = self.run_lab(tcp_detect="timeout")
        self.assertGreater(lab.mn_stack.stats.get("send_failures", 0), 0)      # sends from the vanished address were refused
        self.assertGreater(lab.sim.drop_counts.get(("mn", "src_address_not_local"), 0), 0)

    def test_events_follow_the_common_schema(self):
        lab, m, run = self.run_lab()
        ev = read_csv(run / "events.csv", "events.csv").rows
        self.assertEqual([(e["event_id"], e["kind"]) for e in ev],
                         [("scenario", "scenario_start"), ("handoff-1", "handoff_start"), ("handoff-1", "handoff_end"),
                          ("handoff-2", "handoff_start"), ("handoff-2", "handoff_end"), ("scenario", "scenario_end")])
        from app.compare.events import read_events
        kinds = [e["kind"] for e in read_events(run / "approach_events.csv")]
        for k in ("link_change", "attach", "signal_tx", "signal_rx", "recovered", "conn_opened"):
            self.assertIn(k, kinds)

    def test_runs_are_repeatable(self):
        _, a, ra = self.run_lab(run_id="x1")
        _, b, rb = self.run_lab(run_id="x2")
        for n in ("sent.csv", "received.csv", "echoes.csv", "approach_events.csv"):
            self.assertEqual((ra / n).read_bytes(), (rb / n).read_bytes(), n)


if __name__ == "__main__":
    unittest.main()
