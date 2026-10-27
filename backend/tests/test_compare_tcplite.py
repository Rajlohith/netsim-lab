"""Independent validation of the TcpLite MODEL (before it is used in any comparison)."""
import ipaddress
import unittest

from app.compare import tcplite as T
from app.mobileip.simnet import Sim, SimLink, SimNode, SimRoute


def pair(delay_ms=10, **kw):
    sim = Sim()
    a, b = SimNode(sim, "a"), SimNode(sim, "b")
    link = SimLink("ab", ("a", "e0"), ("b", "e0"), int(delay_ms * 1e6))
    a.add_iface("e0", ipaddress.IPv4Interface("10.0.0.1/24"), link)
    b.add_iface("e0", ipaddress.IPv4Interface("10.0.0.2/24"), link)
    return sim, a, b, link, T.TcpLiteStack(a, sim, **kw), T.TcpLiteStack(b, sim, **kw)


def connect(sim, sa, sb):
    """Returns (client_conn, server_conn, received_by_server, received_by_client)."""
    got_s, got_c, accepted = bytearray(), bytearray(), []

    def accept(conn):
        conn.on_data = got_s.extend
        accepted.append(conn)
    sb.listen(80, accept)
    c = sa.connect("10.0.0.1", "10.0.0.2", 80)
    c.on_data = got_c.extend
    sim.run_until(sim.clock.mono + int(0.2e9))
    return c, accepted[0] if accepted else None, got_s, got_c


class SegmentTests(unittest.TestCase):
    def test_roundtrip_and_checksum(self):
        seg = T.build_segment("10.0.0.1", "10.0.0.2", 1234, 80, 99, 7, T.ACK | T.PSH, b"hello")
        self.assertEqual(len(seg), 25)
        sport, dport, seq, ack, flags, payload = T.parse_segment("10.0.0.1", "10.0.0.2", seg)
        self.assertEqual((sport, dport, seq, ack, flags, payload), (1234, 80, 99, 7, T.ACK | T.PSH, b"hello"))
        bad = bytearray(seg)
        bad[-1] ^= 1
        with self.assertRaises(ValueError):
            T.parse_segment("10.0.0.1", "10.0.0.2", bytes(bad))
        with self.assertRaises(ValueError):
            T.parse_segment("10.0.0.9", "10.0.0.2", seg)                # pseudo-header covers the addresses


class HandshakeAndData(unittest.TestCase):
    def test_handshake_and_bidirectional_in_order_transfer(self):
        sim, a, b, link, sa, sb = pair()
        c, s, got_s, got_c = connect(sim, sa, sb)
        self.assertEqual((c.state, s.state), ("ESTABLISHED", "ESTABLISHED"))
        c.send(b"x" * 3000)                                            # 1400 + 1400 + 200
        s.send(b"reply")
        sim.run_until(sim.clock.mono + int(0.5e9))
        self.assertEqual(bytes(got_s), b"x" * 3000)
        self.assertEqual(bytes(got_c), b"reply")
        self.assertEqual(sa.stats["retransmissions"] + sb.stats["retransmissions"], 0)
        self.assertEqual(len(c.unacked), 0)

    def test_handshake_takes_one_round_trip_to_complete_at_the_client(self):
        sim, a, b, link, sa, sb = pair(delay_ms=10)
        t0 = sim.clock.mono
        times = []
        sb.listen(80, lambda conn: None)
        c = sa.connect("10.0.0.1", "10.0.0.2", 80)
        c.on_connected = lambda conn: times.append(sim.clock.mono - t0)
        sim.run_until(t0 + int(0.2e9))
        self.assertEqual(times, [20_000_000])                          # SYN + SYN-ACK = 2 x 10 ms

    def test_rto_estimator_follows_measured_rtt_and_respects_minimum(self):
        sim, a, b, link, sa, sb = pair(delay_ms=10)
        c, s, *_ = connect(sim, sa, sb)
        c.send(b"ping")
        sim.run_until(sim.clock.mono + int(0.3e9))
        self.assertAlmostEqual(c.srtt, 0.020, places=6)
        self.assertEqual(c.rto, sa.min_rto)                           # srtt + 4*rttvar = 0.06 < 0.2 minimum

    def test_unknown_listener_gets_reset(self):
        sim, a, b, link, sa, sb = pair()
        closed = []
        c = sa.connect("10.0.0.1", "10.0.0.2", 81)
        c.on_closed = closed.append
        sim.run_until(sim.clock.mono + int(0.2e9))
        self.assertEqual(closed, ["reset"])
        self.assertEqual(sb.stats["resets_sent"], 1)

    def test_abort_resets_the_peer_and_state_loss_is_detected(self):
        sim, a, b, link, sa, sb = pair()
        c, s, *_ = connect(sim, sa, sb)
        closed = []
        s.on_closed = closed.append
        c.abort()
        sim.run_until(sim.clock.mono + int(0.2e9))
        self.assertEqual(closed, ["reset"])
        # a peer that lost its state answers data with RST
        c2, s2, *_ = connect.__wrapped__(sim, sa, sb) if hasattr(connect, "__wrapped__") else (None, None)
        sb.conns.clear()
        c3 = sa.connect("10.0.0.1", "10.0.0.2", 80)
        sim.run_until(sim.clock.mono + int(0.2e9))
        sb.conns.clear()
        reasons = []
        c3.on_closed = reasons.append
        c3.send(b"data")
        sim.run_until(sim.clock.mono + int(0.2e9))
        self.assertEqual(reasons, ["reset"])

    def test_corrupted_segments_are_dropped_not_delivered(self):
        sim, a, b, link, sa, sb = pair()
        c, s, got_s, _ = connect(sim, sa, sb)
        orig = b.receive

        def corrupt(packet, ifname):
            p = bytearray(packet)
            p[-1] ^= 0xFF
            import app.mobileip.ipip as ip
            p[10:12] = b"\x00\x00"
            p[10:12] = ip.checksum(bytes(p[:20])).to_bytes(2, "big")
            orig(bytes(p), ifname)
        b.receive = corrupt
        c.send(b"abcd")
        sim.run_until(sim.clock.mono + int(0.1e9))
        self.assertEqual(bytes(got_s), b"")
        self.assertGreaterEqual(sim.drop_counts.get(("b", "bad_tcp"), 0), 1)


class LossRecovery(unittest.TestCase):
    def test_retransmission_schedule_is_exponential_from_the_estimated_rto(self):
        sim, a, b, link, sa, sb = pair()
        c, s, got_s, _ = connect(sim, sa, sb)
        c.send(b"warm")                                                 # RTT sample -> rto = 0.2 s
        sim.run_until(sim.clock.mono + int(0.3e9))
        link.up = False
        t = sim.clock.mono
        c.send(b"lost-at-first")
        at = lambda s: sim.run_until(t + int(s * 1e9)) or sa.stats["retransmissions"]
        self.assertEqual(at(0.19), 0)
        self.assertEqual(at(0.21), 1)                                   # +0.2
        self.assertEqual(at(0.59), 1)
        self.assertEqual(at(0.61), 2)                                   # +0.4
        self.assertEqual(at(1.39), 2)
        self.assertEqual(at(1.41), 3)                                   # +0.8
        link.up = True
        sim.run_until(t + int(3.5e9))                                   # next retry (1.6 s later) gets through
        self.assertEqual(bytes(got_s), b"warmlost-at-first")
        self.assertEqual(len(c.unacked), 0)

    def test_single_lost_segment_is_recovered_in_order(self):
        sim, a, b, link, sa, sb = pair()
        c, s, got_s, _ = connect(sim, sa, sb)
        orig, dropped = b.receive, []

        def drop_second_segment(packet, ifname):
            h, pl = __import__("app.mobileip.ipip", fromlist=["x"]).parse_ipv4(packet)
            _, _, seq, _, _, data = T.parse_segment(h.src, h.dst, pl)
            if data and not dropped and len(data) == 1400 and seq > c.iss + 1400:
                dropped.append(seq)
                return
            orig(packet, ifname)
        b.receive = drop_second_segment
        payload = bytes(range(256)) * 20                                  # 5120 bytes -> 4 segments
        c.send(payload)
        sim.run_until(sim.clock.mono + int(3e9))
        self.assertEqual(len(dropped), 1)
        self.assertEqual(bytes(got_s), payload)                           # in order, nothing lost or duplicated
        self.assertGreaterEqual(sa.stats["retransmissions"], 1)

    def test_syn_retransmission_then_success(self):
        sim, a, b, link, sa, sb = pair()
        sb.listen(80, lambda conn: None)
        link.up = False
        c = sa.connect("10.0.0.1", "10.0.0.2", 80)
        done = []
        c.on_connected = lambda conn: done.append(sim.clock.mono)
        sim.run_until(sim.clock.mono + int(0.9e9))
        link.up = True
        sim.run_until(sim.clock.mono + int(3e9))
        self.assertEqual(c.state, "ESTABLISHED")
        self.assertGreaterEqual(sa.stats["retransmissions"], 1)

    def test_syn_gives_up_after_the_retry_limit(self):
        sim, a, b, link, sa, sb = pair()
        link.up = False
        c = sa.connect("10.0.0.1", "10.0.0.2", 80)
        closed, t0 = [], sim.clock.mono
        c.on_closed = lambda r: closed.append((r, (sim.clock.mono - t0) / 1e9))
        sim.run_until(t0 + int(40e9))
        self.assertEqual(closed[0][0], "timeout")
        self.assertAlmostEqual(closed[0][1], 31.0, delta=0.01)             # 1+2+4+8+8+8 s

    def test_established_connection_times_out_when_the_path_never_returns(self):
        sim, a, b, link, sa, sb = pair()
        c, s, *_ = connect(sim, sa, sb)
        c.send(b"warm")
        sim.run_until(sim.clock.mono + int(0.3e9))
        link.up = False
        closed, t0 = [], sim.clock.mono
        c.on_closed = lambda r: closed.append((r, (sim.clock.mono - t0) / 1e9))
        c.send(b"never delivered")
        sim.run_until(t0 + int(30e9))
        self.assertEqual(closed[0][0], "timeout")
        self.assertAlmostEqual(closed[0][1], 20.6, delta=0.01)            # 0.2+0.4+0.8+1.6+3.2+6.4+8.0 s


class AddressChange(unittest.TestCase):
    def test_connection_to_a_vanished_address_never_recovers(self):
        """The core reason the TCP baseline must reconnect: the 4-tuple is bound to the old address."""
        sim, a, b, link, sa, sb = pair()
        c, s, got_s, _ = connect(sim, sa, sb)
        b.ifaces["e0"][0].ip                                               # keep reference
        a.ifaces["e0"] = (ipaddress.IPv4Interface("10.0.0.99/24"), link)    # a's address changes
        c.send(b"from the old address")
        sim.run_until(sim.clock.mono + int(5e9))
        self.assertEqual(c.state, "ESTABLISHED")
        self.assertEqual(bytes(got_s), b"")                                 # data never arrives, retransmitting forever


if __name__ == "__main__":
    unittest.main()
