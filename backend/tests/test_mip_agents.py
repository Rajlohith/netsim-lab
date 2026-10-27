"""Protocol behaviour of HomeAgent / MobileNode, driven directly (deterministic clock, no sockets)."""
import ipaddress
import unittest

from app.mobileip import messages as M
from tests.mip_helpers import AGENT, HOME, KEY, SA, Pair, raw_request

A = ipaddress.IPv4Address


class RegistrationTests(unittest.TestCase):
    def test_successful_registration_creates_binding_and_tunnel(self):
        p = Pair()
        p.attach("10.10.4.2")
        self.assertEqual(p.mn.state, "registered")
        b = p.ha.bindings.get(HOME)
        self.assertEqual((str(b.care_of_address), b.lifetime_s), ("10.10.4.2", 60))
        self.assertEqual(p.ha_dp.tunnels, {A(HOME): A("10.10.4.2")})
        self.assertEqual(p.mn_dp.care_of, A("10.10.4.2"))                # MN can decapsulate before HA tunnels
        self.assertEqual(p.mn_dp.calls[0], ("set_care_of", "10.10.4.2"))
        self.assertIn("binding_created", p.events.kinds("ha"))
        self.assertEqual(p.mn.granted_lifetime_s, 60)

    def test_home_agent_may_shorten_lifetime(self):
        p = Pair(max_lifetime=20, lifetime=600)
        p.attach()
        self.assertEqual(p.ha.bindings.get(HOME).lifetime_s, 20)
        self.assertEqual(p.mn.granted_lifetime_s, 20)

    def test_movement_updates_binding(self):
        p = Pair()
        p.attach("10.10.4.2")
        p.attach("10.10.5.2")
        b = p.ha.bindings.get(HOME)
        self.assertEqual((str(b.care_of_address), b.registrations), ("10.10.5.2", 2))
        self.assertEqual(p.ha_dp.tunnels[A(HOME)], A("10.10.5.2"))
        self.assertEqual(len(p.ha.bindings), 1)                          # one binding per home address
        self.assertEqual(p.events.kinds("ha").count("binding_updated"), 1)

    def test_deregistration_removes_binding_and_tunnel(self):
        p = Pair()
        p.attach()
        p.exchange(p.mn.deregister())
        self.assertEqual(len(p.ha.bindings), 0)
        self.assertEqual(p.ha_dp.tunnels, {})
        self.assertEqual((p.mn.state, p.mn_dp.care_of), ("detached", None))
        self.assertIn("binding_removed", p.events.kinds("ha"))

    def test_binding_expires_without_renewal_and_tunnel_is_removed(self):
        p = Pair(lifetime=2, reregister_fraction=10.0)                    # MN never renews
        p.attach()
        p.advance(1.9)
        self.assertEqual(len(p.ha.bindings), 1)
        p.advance(0.3)
        self.assertEqual(len(p.ha.bindings), 0)
        self.assertEqual(p.ha_dp.tunnels, {})
        self.assertIn("binding_expired", p.events.kinds("ha"))

    def test_reregistration_keeps_binding_alive(self):
        p = Pair(lifetime=2)
        p.attach()
        p.advance(7.0)
        self.assertEqual(len(p.ha.bindings), 1)
        self.assertGreaterEqual(p.events.kinds("mn").count("reregister"), 5)
        self.assertEqual(p.ha.bindings.get(HOME).registrations, 1 + p.events.kinds("mn").count("reregister"))
        self.assertEqual(p.mn.state, "registered")

    def test_shutdown_removes_everything(self):
        p = Pair()
        p.attach()
        p.ha.shutdown()
        self.assertEqual((len(p.ha.bindings), p.ha_dp.tunnels), (0, {}))
        p.ha.shutdown()                                                   # idempotent
        self.assertIn("tunnel_stats", p.events.kinds("ha"))


class SecurityTests(unittest.TestCase):
    def test_wrong_key_rejected_131_and_no_binding(self):
        p = Pair(mn_key=b"not-the-shared-key-!!")
        p.attach()
        self.assertEqual(len(p.ha.bindings), 0)
        self.assertEqual(p.ha_dp.calls, [])
        self.assertIn("auth_failure", p.events.kinds("ha"))
        # the 131 reply is signed with the HA's key, so the MN cannot authenticate it and must not trust it:
        # it keeps retrying and only gives up by timeout
        self.assertIn("reply_auth_invalid", p.events.kinds("mn"))
        self.assertEqual(p.mn.state, "registering")
        p.advance(16.0)
        self.assertEqual(p.mn.state, "failed")
        self.assertIn("reg_timeout", p.events.kinds("mn"))
        self.assertEqual(len(p.ha.bindings), 0)

    def test_unknown_mobile_node_rejected(self):
        p = Pair()
        data, _ = raw_request(home="10.10.100.77")
        out = p.ha.handle_datagram(data, ("10.10.4.2", 4341))
        self.assertEqual(M.decode_reply(out[0].data).code, M.CODE_HA_MN_AUTH_FAILED)
        self.assertEqual(len(p.ha.bindings), 0)

    def test_unauthenticated_request_rejected(self):
        p = Pair()
        data, _ = raw_request(sa=None)
        self.assertEqual(M.decode_reply(p.ha.handle_datagram(data, ("1.1.1.1", 1))[0].data).code, 131)

    def test_replayed_request_is_rejected_133(self):
        p = Pair()
        p.attach("10.10.4.2")
        original = next(d for kind, d in p.wire if kind == "req")
        out = p.ha.handle_datagram(original, ("6.6.6.6", 999))           # attacker replays a captured request
        rep = M.decode_reply(out[0].data)
        self.assertEqual(rep.code, M.CODE_HA_IDENT_MISMATCH)
        self.assertIn("replay_rejected", p.events.kinds("ha"))
        self.assertEqual(str(p.ha.bindings.get(HOME).care_of_address), "10.10.4.2")

    def test_replay_after_movement_cannot_move_binding_back(self):
        p = Pair()
        p.attach("10.10.4.2")
        first_req = next(d for kind, d in p.wire if kind == "req")
        p.attach("10.10.5.2")
        p.ha.handle_datagram(first_req, ("6.6.6.6", 999))
        self.assertEqual(str(p.ha.bindings.get(HOME).care_of_address), "10.10.5.2")
        self.assertEqual(p.ha_dp.tunnels[A(HOME)], A("10.10.5.2"))

    def test_stale_identification_outside_window_rejected(self):
        p = Pair()
        data, _ = raw_request(ident=M.ident_from_wall_ns(p.clock.wall_ns() - 60 * 10**9))
        self.assertEqual(M.decode_reply(p.ha.handle_datagram(data, ("1.1.1.1", 1))[0].data).code, 133)
        data, _ = raw_request(ident=M.ident_from_wall_ns(p.clock.wall_ns() + 60 * 10**9))
        self.assertEqual(M.decode_reply(p.ha.handle_datagram(data, ("1.1.1.1", 1))[0].data).code, 133)

    def test_clock_skewed_mobile_node_resynchronises(self):
        p = Pair(skew_s=3600.0)
        p.attach()
        self.assertEqual(p.mn.state, "registered")
        ks = p.events.kinds()
        self.assertEqual((ks.count("replay_rejected"), ks.count("resync_clock")), (1, 1))
        self.assertAlmostEqual(p.mn.clock_offset_ns / 1e9, -3600.0, delta=1.0)

    def test_tampered_request_fails_authentication(self):
        p = Pair()
        data, _ = raw_request()
        bad = bytearray(data)
        bad[18] ^= 0x01                                                   # flip a care-of address bit
        self.assertEqual(M.decode_reply(p.ha.handle_datagram(bytes(bad), ("1.1.1.1", 1))[0].data).code, 131)
        self.assertEqual(len(p.ha.bindings), 0)

    def test_mn_ignores_forged_and_stale_replies(self):
        p = Pair()
        p.attach()
        rep = M.RegistrationReply(0, 9999, A(HOME), A(AGENT), 42)
        self.assertEqual(p.mn.handle_datagram(M.encode_reply(rep, None), ("6.6.6.6", 1)), [])      # unsigned
        forged = M.encode_reply(rep, M.SecurityAssociation(256, b"attacker-key-0123456789"))
        self.assertEqual(p.mn.handle_datagram(forged, ("6.6.6.6", 1)), [])
        self.assertEqual(p.mn.granted_lifetime_s, 60)
        valid_but_unsolicited = M.encode_reply(rep, SA)                   # right key, no matching request
        p.mn.handle_datagram(valid_but_unsolicited, ("6.6.6.6", 1))
        self.assertEqual(p.mn.granted_lifetime_s, 60)
        self.assertGreaterEqual(p.events.kinds("mn").count("reply_auth_invalid"), 3)


class ValidationTests(unittest.TestCase):
    n = 0

    def reply_code(self, p, **kw):
        ValidationTests.n += 1                                    # every request needs a fresh, newer identification
        kw.setdefault("ident", M.ident_from_wall_ns(p.clock.wall_ns()) + ValidationTests.n)
        data, _ = raw_request(**kw)
        out = p.ha.handle_datagram(data, ("10.10.4.2", 4341))
        return M.decode_reply(out[0].data).code if out else None

    def test_rejections(self):
        p = Pair()
        self.assertEqual(self.reply_code(p, agent="10.10.100.9"), M.CODE_HA_UNKNOWN_HA)
        self.assertEqual(self.reply_code(p, flags=0), M.CODE_HA_UNSPECIFIED)               # needs D (co-located CoA)
        self.assertEqual(self.reply_code(p, flags=M.FLAG_D | M.FLAG_T), M.CODE_HA_UNSPECIFIED)   # no reverse tunnel
        self.assertEqual(self.reply_code(p, flags=M.FLAG_D | M.FLAG_G), M.CODE_HA_UNSPECIFIED)
        self.assertEqual(self.reply_code(p, coa="10.10.100.5"), M.CODE_HA_POORLY_FORMED)    # CoA inside home network
        self.assertEqual(self.reply_code(p, coa="0.0.0.0"), M.CODE_HA_POORLY_FORMED)
        self.assertEqual(self.reply_code(p, coa="224.0.0.1"), M.CODE_HA_POORLY_FORMED)
        self.assertEqual(len(p.ha.bindings), 0)
        self.assertEqual(p.ha_dp.calls, [])

    def test_deregistration_requires_home_address_as_coa(self):
        p = Pair()
        p.attach()
        self.assertEqual(self.reply_code(p, lifetime=0, coa="10.10.4.2", ident=M.ident_from_wall_ns(p.clock.wall_ns() + 10**6)),
                         M.CODE_HA_POORLY_FORMED)
        self.assertEqual(len(p.ha.bindings), 1)

    def test_s_flag_gives_code_1_and_still_one_binding(self):
        p = Pair()
        self.assertEqual(self.reply_code(p, flags=M.FLAG_D | M.FLAG_S), M.CODE_ACCEPTED_NO_SIMULTANEOUS)
        self.assertEqual(len(p.ha.bindings), 1)

    def test_garbage_is_dropped_or_answered_134(self):
        p = Pair()
        self.assertEqual(p.ha.handle_datagram(b"\xff" * 5, ("1.1.1.1", 1)), [])
        data, _ = raw_request()
        out = p.ha.handle_datagram(data[:24] + b"\x63\x01", ("1.1.1.1", 1))    # bad extension
        self.assertEqual(M.decode_reply(out[0].data).code, M.CODE_HA_POORLY_FORMED)
        self.assertEqual(p.events.kinds("ha").count("request_malformed"), 2)

    def test_data_plane_failure_gives_130_and_no_binding(self):
        p = Pair()
        p.ha_dp.fail = True
        p.attach()
        self.assertEqual(p.mn.state, "failed")
        self.assertEqual(len(p.ha.bindings), 0)
        self.assertIn("dataplane_error", p.events.kinds("ha"))
        self.assertEqual([r["code"] for r in p.events.rows if r["kind"] == "reg_reply_received"], [130])


class RetransmissionTests(unittest.TestCase):
    def test_lost_request_is_retransmitted_then_succeeds(self):
        p = Pair(retransmit_initial_s=1.0)
        outs = p.mn.attach("10.10.4.2")                                   # first request "lost" (never delivered)
        p.clock.advance(1.01)
        p.exchange(p.mn.poll())                                           # retransmission delivered
        self.assertEqual(p.mn.state, "registered")
        ks = p.events.kinds("mn")
        self.assertEqual((ks.count("reg_retransmit"), ks.count("reg_request_sent")), (1, 2))

    def test_backoff_and_final_timeout(self):
        p = Pair(retransmit_initial_s=1.0, retransmit_max_s=8.0, max_retransmits=3)
        p.mn.attach("10.10.4.2")
        times = []
        for _ in range(400):
            p.clock.advance(0.05)
            if p.mn.poll():
                times.append(round((p.clock.m - 1_000_000_000) / 1e9, 2))
        self.assertEqual(len(times), 3)                                   # 3 retransmissions after the first send
        self.assertAlmostEqual(times[0], 1.0, delta=0.06)
        self.assertAlmostEqual(times[1], 3.0, delta=0.12)                 # +2 s
        self.assertAlmostEqual(times[2], 7.0, delta=0.2)                  # +4 s
        self.assertEqual(p.mn.state, "failed")
        self.assertIn("reg_timeout", p.events.kinds("mn"))
        self.assertIn("reg_failed", p.events.kinds("mn"))

    def test_late_reply_to_first_attempt_still_accepted(self):
        p = Pair(retransmit_initial_s=1.0)
        first = p.mn.attach("10.10.4.2")
        p.clock.advance(1.01)
        p.mn.poll()                                                       # retransmitted, but we deliver the FIRST one
        for rep in p.ha.handle_datagram(first[0].data, ("10.10.4.2", 4341)):
            p.mn.handle_datagram(rep.data, (AGENT, 4340))
        self.assertEqual(p.mn.state, "registered")


if __name__ == "__main__":
    unittest.main()
