"""Metrics against hand-computed observations (no sockets, fully deterministic)."""
import tempfile
import unittest
from pathlib import Path

from app.traffic.loader import read_csv
from app.traffic.metrics import compute_metrics

MS = 1_000_000
BOOT = "boot-1"


def meta(role, status="completed", boot=BOOT, **extra):
    d = {"role": role, "status": status, "session_id": 1, "scenario_id": "s", "run_id": "r",
         "clock": {"boot_id": boot}, "counters": {}, "params": {}}
    d.update(extra)
    return d


def S(seq, t_ms, size=64, errno=0, sched=None):
    t = int(t_ms * MS)
    return {"seq": seq, "scheduled_mono_ns": t if sched is None else int(sched * MS), "send_mono_ns": t,
            "send_wall_ns": t, "size_bytes": size, "send_errno": errno}


def R(seq, recv_ms, send_ms, size=64, session=1):
    return {"seq": seq, "session_id": session, "recv_mono_ns": int(recv_ms * MS), "recv_wall_ns": 0,
            "hdr_send_mono_ns": int(send_ms * MS), "hdr_send_wall_ns": 0, "size_bytes": size,
            "src_addr": "x", "src_port": 1}


def E(seq, recv_ms, size=64):
    return {"seq": seq, "recv_mono_ns": int(recv_ms * MS), "recv_wall_ns": 0, "size_bytes": size}


def metas(n_sent):
    return (meta("sender", counters={"sent_rows": n_sent}), meta("receiver"))


class MetricsTests(unittest.TestCase):
    def test_loss_from_sequence_numbers(self):
        sent = [S(i, i * 10) for i in range(10)]
        rx = [R(i, i * 10 + 5, i * 10) for i in (0, 1, 2, 5, 6, 9)]
        m = compute_metrics(sent, rx, None, None, *metas(10))
        L = m["loss"]
        self.assertEqual((L["basis"], L["exact"], L["sent_ok"], L["delivered"], L["lost"]),
                         ("sender_log", True, 10, 6, 4))
        self.assertEqual(L["lost_seqs"], [3, 4, 7, 8])
        self.assertEqual(L["loss_percent"], 40.0)
        self.assertEqual([(b["first_seq"], b["last_seq"], b["count"]) for b in L["bursts"]], [(3, 4, 2), (7, 8, 2)])
        self.assertEqual(L["longest_burst_packets"], 2)

    def test_send_errors_are_not_counted_as_network_loss(self):
        sent = [S(0, 0), S(1, 10, errno=101), S(2, 20)]
        rx = [R(0, 5, 0), R(2, 25, 20)]
        L = compute_metrics(sent, rx, None, None, *metas(3))["loss"]
        self.assertEqual((L["sent_ok"], L["lost"], L["send_errors"]), (2, 0, 1))

    def test_duplicates_and_reordering(self):
        sent = [S(i, i * 10) for i in range(4)]
        rx = [R(0, 5, 0), R(2, 26, 20), R(1, 27, 10), R(3, 35, 30), R(3, 36, 30)]
        m = compute_metrics(sent, rx, None, None, *metas(4))
        self.assertEqual((m["duplicates"], m["reordered_packets"], m["loss"]["lost"]), (1, 1, 0))

    def test_interarrival_gaps(self):
        sent = [S(i, i * 10) for i in range(5)]
        rx = [R(0, 5, 0), R(1, 15, 10), R(2, 25, 20), R(3, 75, 30), R(4, 85, 40)]
        g = compute_metrics(sent, rx, None, None, *metas(5))["interarrival_gaps_ms"]
        self.assertEqual((g["count"], g["min"], g["max"], g["mean"]), (4, 10.0, 50.0, 20.0))
        self.assertEqual((g["max_gap_detail"]["after_seq"], g["max_gap_detail"]["before_seq"]), (2, 3))

    def test_rtt_uses_sender_clock_only(self):
        sent = [S(0, 0), S(1, 10), S(2, 20)]
        rx = [R(i, i * 10 + 20, i * 10) for i in range(3)]
        echoes = [E(0, 40), E(1, 52)]                      # seq 2 never echoed
        m = compute_metrics(sent, rx, echoes, None, *metas(3))
        self.assertEqual((m["rtt_ms"]["count"], m["rtt_ms"]["min"], m["rtt_ms"]["max"], m["rtt_ms"]["mean"]),
                         (2, 40.0, 42.0, 41.0))
        self.assertEqual(m["rtt_info"]["delivered_without_echo"], 1)
        self.assertAlmostEqual(m["jitter"]["rtt_delta_mean_ms"], 2.0)

    def test_duplicate_echo_counted_once_and_orphan_excluded(self):
        sent = [S(0, 0)]
        m = compute_metrics(sent, [R(0, 20, 0)], [E(0, 40), E(0, 90), E(9, 50)], None, *metas(1))
        self.assertEqual(m["rtt_ms"]["count"], 1)
        self.assertEqual(m["rtt_ms"]["max"], 40.0)
        self.assertTrue(any("no sender row" in w for w in m["warnings"]))

    def test_rfc3550_jitter_hand_computed(self):
        send = [0, 10, 20, 30, 40]
        recv = [5, 16, 25, 38, 45]                          # transit 5,6,5,8,5 -> |D| 1,1,3,3
        sent = [S(i, t) for i, t in enumerate(send)]
        rx = [R(i, r, s) for i, (r, s) in enumerate(zip(recv, send))]
        j = compute_metrics(sent, rx, None, None, *metas(5))["jitter"]
        self.assertAlmostEqual(j["rfc3550_final_ms"], 0.469711, places=5)
        self.assertEqual(j["samples"], 4)

    def test_constant_delay_has_zero_jitter_even_with_clock_offset(self):
        # receiver clock 1e9 ns ahead of sender: transit differences cancel the offset
        sent = [S(i, i * 10) for i in range(20)]
        rx = [R(i, i * 10 + 1000 + 7, i * 10) for i in range(20)]
        self.assertEqual(compute_metrics(sent, rx, None, None, *metas(20))["jitter"]["rfc3550_final_ms"], 0.0)

    def test_owd_only_within_same_clock_domain(self):
        sent = [S(i, i * 10) for i in range(3)]
        rx = [R(i, i * 10 + 7, i * 10) for i in range(3)]
        same = compute_metrics(sent, rx, None, None, *metas(3))
        self.assertEqual((same["owd_ms"]["min"], same["owd_ms"]["max"]), (7.0, 7.0))
        other = compute_metrics(sent, rx, None, None, meta("sender", counters={"sent_rows": 3}),
                                meta("receiver", boot="boot-2"))
        self.assertIsNone(other["owd_ms"])
        self.assertIn("different domains", other["owd_unavailable_reason"])
        none = compute_metrics(sent, rx, None, None, None, None)
        self.assertIsNone(none["owd_ms"])

    def test_throughput(self):
        sent = [S(i, i * 10, size=100) for i in range(11)]
        rx = [R(i, i * 10 + 5, i * 10, size=100) for i in range(11)]
        tp = compute_metrics(sent, rx, None, None, *metas(11))["throughput"]
        self.assertEqual(tp["offered_bps"], 80000.0)
        self.assertEqual(tp["goodput_bps"], 80000.0)
        self.assertEqual((tp["sent_bytes"], tp["received_bytes"]), (1100, 1100))
        self.assertEqual(len(tp["timeseries"]), 1)
        self.assertEqual(tp["timeseries"][0]["packets"], 11)

    def test_throughput_timeseries_shows_measured_silence(self):
        rx = [R(i, t, t) for i, t in enumerate([0, 100, 2500])]
        ts = compute_metrics(None, rx, None, None, None, None, bin_s=1.0)["throughput"]["timeseries"]
        self.assertEqual([b["packets"] for b in ts], [2, 0, 1])

    def test_receiver_only_gives_lower_bound_not_exact(self):
        rx = [R(i, i * 10, i * 10) for i in (0, 1, 2, 6, 7)]
        m = compute_metrics(None, rx, None, None, None, meta("receiver"))
        L = m["loss"]
        self.assertEqual((L["basis"], L["exact"], L["lost"]), ("receiver_gaps", False, 3))
        self.assertIsNone(L["sent_ok"])
        self.assertTrue(any("LOWER BOUND" in w for w in m["warnings"]))

    def test_unfinalised_sender_makes_loss_inexact_and_run_incomplete(self):
        sent = [S(i, i * 10) for i in range(5)]
        rx = [R(i, i * 10 + 5, i * 10) for i in range(4)] + [R(5, 55, 50)]   # seq 5 absent from sender log
        m = compute_metrics(sent, rx, None, None, meta("sender", status="running"), meta("receiver"))
        self.assertEqual(m["run_status"], "incomplete")
        self.assertFalse(m["loss"]["exact"])
        self.assertEqual(m["loss"]["received_not_in_sent_log"], 1)
        self.assertTrue(any("not exact" in w for w in m["warnings"]))

    def test_truncated_sender_log_detected_via_counters(self):
        sent = [S(i, i * 10) for i in range(3)]
        m = compute_metrics(sent, [R(i, i * 10 + 5, i * 10) for i in range(3)], None, None,
                            meta("sender", counters={"sent_rows": 5}), meta("receiver"))
        self.assertFalse(m["loss"]["exact"])

    def test_nothing_is_fabricated_from_empty_input(self):
        m = compute_metrics([], [], [], None, None, None)
        self.assertIsNone(m["loss"]["loss_percent"])
        self.assertEqual(m["interarrival_gaps_ms"]["count"], 0)
        self.assertIsNone(m["interarrival_gaps_ms"]["mean"])
        self.assertIsNone(m["throughput"]["goodput_bps"])
        self.assertIsNone(m["jitter"]["rfc3550_final_ms"])
        self.assertEqual(m["rtt_ms"]["count"], 0)

    def test_other_sessions_ignored(self):
        sent = [S(0, 0), S(1, 10)]
        rx = [R(0, 5, 0), R(1, 15, 10), R(0, 6, 0, session=99)]
        m = compute_metrics(sent, rx, None, None, *metas(2))
        self.assertEqual(m["duplicates"], 0)
        self.assertTrue(any("other sessions" in w for w in m["warnings"]))

    def test_schedule_slip(self):
        sent = [S(0, 0.5, sched=0), S(1, 10.2, sched=10)]
        slip = compute_metrics(sent, [], None, None, *metas(2))["sender_schedule_slip_ms"]
        self.assertAlmostEqual(slip["max"], 0.5)


def handoff_fixture():
    """Probes every 10 ms for 2 s; seq 100..119 (sent 1.00-1.19 s) lost; RTT 10 ms before, 30 ms after."""
    sent = [S(i, i * 10) for i in range(200)]
    lost = set(range(100, 120))
    rx = [R(i, i * 10 + 5, i * 10) for i in range(200) if i not in lost]
    echoes = [E(i, i * 10 + (10 if i < 100 else 30)) for i in range(200) if i not in lost]
    events = [
        {"event_id": "handoff-1", "kind": "handoff_start", "mono_ns": 1000 * MS, "wall_ns": 0, "detail": "mn:eth-a down"},
        {"event_id": "handoff-1", "kind": "handoff_end", "mono_ns": 1004 * MS, "wall_ns": 0, "detail": "mn:eth-a down"},
    ]
    return sent, rx, echoes, events


class HandoffMetricsTests(unittest.TestCase):
    def run_it(self, boot_events=BOOT):
        sent, rx, echoes, events = handoff_fixture()
        return compute_metrics(sent, rx, echoes, events, *metas(200), {"clock": {"boot_id": boot_events}})

    def test_interruption_and_delay_change(self):
        h = self.run_it()["handoffs"][0]
        self.assertEqual(h["loss_burst"]["packets"], 20)
        self.assertEqual((h["loss_burst"]["first_seq"], h["loss_burst"]["last_seq"]), (100, 119))
        self.assertTrue(h["recovered"])
        self.assertAlmostEqual(h["interruption_ms"], 210.0)          # arrival of seq 99 -> seq 120
        self.assertEqual(h["action_duration_ms"], 4.0)
        self.assertEqual((h["rtt_median_before_ms"], h["rtt_median_after_ms"]), (10.0, 30.0))
        self.assertAlmostEqual(h["max_interarrival_gap_in_window_ms"], 210.0)

    def test_handoff_without_loss_reports_no_burst(self):
        sent, rx, echoes, events = handoff_fixture()
        rx = [R(i, i * 10 + 5, i * 10) for i in range(200)]
        m = compute_metrics(sent, rx, None, events, *metas(200), {"clock": {"boot_id": BOOT}})
        h = m["handoffs"][0]
        self.assertIsNone(h["loss_burst"])
        self.assertIsNone(h["interruption_ms"])
        self.assertIsNone(h["rtt_median_before_ms"])               # no echoes -> nothing invented

    def test_unprovable_clock_domain_yields_no_handoff_metrics(self):
        m = self.run_it(boot_events="different")
        self.assertIsNone(m["handoffs"])
        self.assertIn("clock domains", m["handoffs_unavailable_reason"])
        m2 = compute_metrics(*handoff_fixture(), *metas(200), None)
        self.assertIsNone(m2["handoffs"])

    def test_unfinished_handoff_is_flagged(self):
        sent, rx, echoes, events = handoff_fixture()
        m = compute_metrics(sent, rx, echoes, events[:1], *metas(200), {"clock": {"boot_id": BOOT}})
        h = m["handoffs"][0]
        self.assertTrue(h["end_missing"])
        self.assertIsNone(h["action_duration_ms"])


class LoaderTests(unittest.TestCase):
    def test_truncated_tail_and_malformed_rows(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "echoes.csv"
            p.write_text("seq,recv_mono_ns,recv_wall_ns,size_bytes\n1,100,200,64\nbad,row\n2,x,3,64\n3,300,400,6")
            r = read_csv(p, "echoes.csv")
            self.assertTrue(r.truncated_tail)
            self.assertEqual(r.skipped_rows, 2)
            self.assertEqual([x["seq"] for x in r.rows], [1])      # partial last line never parsed
            self.assertFalse(read_csv(Path(d) / "nope.csv", "echoes.csv").present)

    def test_wrong_header_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "echoes.csv"
            p.write_text("a,b\n1,2\n")
            with self.assertRaises(ValueError):
                read_csv(p, "echoes.csv")


if __name__ == "__main__":
    unittest.main()
