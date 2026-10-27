"""The shared application logic (apps.py) over REAL kernel TCP on loopback, plus protocol edge cases."""
import tempfile
import unittest

from app.compare import apps
from app.compare.experiment import ExperimentSpec
from app.compare.loopback import run_loopback_tcp
from app.compare.normalize import normalize
from app.traffic import wire
from app.traffic.loader import read_csv


class FramerTests(unittest.TestCase):
    def test_partial_and_coalesced_frames(self):
        f = apps.Framer()
        a, b = apps.frame(b"D", b"x" * 10), apps.frame(b"A", b"yy")
        blob = a + b
        out = []
        for i in range(0, len(blob), 3):                          # arbitrary TCP segmentation
            out += f.feed(blob[i:i + 3])
        self.assertEqual(out, [b"D" + b"x" * 10, b"Ayy"])
        self.assertEqual(f.buf, b"")

    def test_oversized_frame_is_rejected(self):
        with self.assertRaises(ValueError):
            apps.Framer().feed((1 << 21).to_bytes(4, "big") + b"x")


class FakeConn:
    def __init__(self):
        self.sent, self.aborted = [], False
        self.on_connected = self.on_data = self.on_closed = None

    def send(self, d):
        self.sent.append(d)
        return True

    def abort(self):
        self.aborted = True


class FakeSched:
    def __init__(self):
        self.t, self.q = 0, []

    def now_ns(self):
        return self.t

    def wall_ns(self):
        return self.t

    def call_later(self, d, fn):
        self.q.append((self.t + int(d * 1e9), fn))

    def call_at(self, ns, fn):
        self.q.append((ns, fn))

    def run(self, until_ns):
        while self.q:
            self.q.sort(key=lambda x: x[0])
            if self.q[0][0] > until_ns:
                break
            t, fn = self.q.pop(0)
            self.t = t
            fn()
        self.t = until_ns


def hooks():
    h = {"events": [], "recv": [], "sent": [], "echo": []}
    h.update(sent_fn=None)
    return {"sent": lambda *a: h["sent"].append(a), "echo": lambda *a: h["echo"].append(a),
            "recv": lambda p, size, dup: h["recv"].append((p.seq, dup)), "event": lambda k, d="": h["events"].append((k, d)),
            "signal_tx": lambda n: None}, h


class ProtocolEdgeCases(unittest.TestCase):
    def test_server_rejects_wrong_session_and_garbage(self):
        hk, h = hooks()
        s = apps.StreamProbeServer(FakeSched(), 10, 1, 64, hk)
        c = FakeConn()
        s.on_accept(c)
        c.on_data(apps.frame(b"H", __import__("struct").pack("!IQ", 0xDEAD, 0)))
        self.assertTrue(c.aborted)
        c2 = FakeConn()
        s.on_accept(c2)
        c2.on_data((1 << 25).to_bytes(4, "big") + b"zzz")           # absurd frame length
        self.assertTrue(c2.aborted)

    def test_server_replaces_stale_connection_and_resends_from_hello(self):
        sched = FakeSched()
        hk, h = hooks()
        s = apps.StreamProbeServer(sched, 10, 1, 64, hk)
        s.start(0)
        sched.run(int(0.55e9))                                       # probes 0..5 buffered (nobody connected yet)
        old, new = FakeConn(), FakeConn()
        s.on_accept(old)
        import struct
        old.on_data(apps.frame(b"H", struct.pack("!IQ", apps.SESSION, 0)))
        self.assertEqual(len(old.sent), 6)
        s.on_accept(new)
        new.on_data(apps.frame(b"H", struct.pack("!IQ", apps.SESSION, 4)))   # client already has 0..3
        self.assertTrue(old.aborted)
        self.assertEqual(len(new.sent), 2)                           # only seq 4 and 5 again
        self.assertEqual(s.resends, 2)

    def test_client_without_reconnect_support_reports_failure(self):
        sched = FakeSched()
        hk, h = hooks()
        conns = []
        client = apps.StreamProbeClient(sched, lambda ip: conns.append(FakeConn()) or conns[-1], hk, reconnect=False)
        client.start("1.1.1.1")
        conns[0].on_closed("reset")
        self.assertIn("failure", [k for k, _ in h["events"]])
        self.assertEqual(len(conns), 1)                                # no new connection attempt

    def test_duplicates_are_flagged_not_counted_as_progress(self):
        sched = FakeSched()
        hk, h = hooks()
        conns = []
        client = apps.StreamProbeClient(sched, lambda ip: conns.append(FakeConn()) or conns[-1], hk)
        client.start("1.1.1.1")
        c = conns[0]
        for seq in (0, 1, 1, 2):
            c.on_data(apps.frame(b"D", wire.encode(wire.TYPE_PROBE, apps.SESSION, seq, 1, 1, 64)))
        self.assertEqual(h["recv"], [(0, False), (1, False), (1, True), (2, False)])
        self.assertEqual(client.next_expected, 3)


class RealTcpValidation(unittest.TestCase):
    """Real kernel TCP: the application keeps its SESSION alive although every connection is replaced."""

    def run_it(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        spec = ExperimentSpec("tcp-reconnect", environment="loopback", duration_s=2.5, rate_pps=100,
                              moves=((1.0, "eth-b"), (1.8, "eth-a")), **kw)
        run = run_loopback_tcp(spec, tmp.name)
        return normalize(run), run

    def test_event_notification_resumes_without_loss_or_duplicates(self):
        m, run = self.run_it()
        self.assertEqual((m["loss"]["lost"], m["loss"]["sent"], m["loss"]["exact"]), (0, 200, True))
        self.assertEqual((m["duplicates"], m["reordered_packets"]), (0, 0))
        self.assertEqual(m["continuity"]["connections_opened"], 3)            # initial + one per handoff
        self.assertFalse(m["continuity"]["transport_connection_preserved"])   # the TRANSPORT connection was not preserved
        self.assertTrue(m["continuity"]["all_handoffs_recovered"])
        self.assertFalse(m["timing_comparable"])
        rx = read_csv(run / "received.csv", "received.csv").rows
        self.assertEqual([r["seq"] for r in rx], list(range(200)))             # strictly in order, nothing missing
        self.assertEqual(sorted({r["src_addr"] for r in rx}), ["127.0.0.1"])

    def test_stall_timeout_policy_is_slower_than_notification(self):
        fast, _ = self.run_it(tcp_detect="event")
        slow, _ = self.run_it(tcp_detect="timeout", tcp_detect_timeout_s=0.25)
        self.assertEqual(slow["loss"]["lost"], 0)
        self.assertGreater(slow["handoff_summary"]["service_gap_ms_mean"], 200)
        self.assertLess(fast["handoff_summary"]["service_gap_ms_mean"], 100)
        self.assertGreater(slow["handoff_summary"]["handoff_latency_ms_mean"], fast["handoff_summary"]["handoff_latency_ms_mean"] + 150)


if __name__ == "__main__":
    unittest.main()
