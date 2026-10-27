"""The simplified route-optimisation MODEL: Return Routability security properties and its performance trade-off."""
import ipaddress
import tempfile
import unittest
from dataclasses import replace

from app.compare.experiment import DEFAULT_TOPOLOGY, ExperimentSpec
from app.compare.runner import run_experiment
from app.mobileip import ro
from app.mobileip.mobility_sim import HOME_ADDR, MobilitySpec
from app.mobileip.ro_sim import RoSim
from app.traffic.analyze import analyze_run
from tests.mip_helpers import FakeClock, ListEvents

A = ipaddress.IPv4Address
CN = "10.10.1.2"
SECRET = b"unit-test-cn-secret-0123456789"


class Pair:
    def __init__(self, **mn_kw):
        self.clock, self.events = FakeClock(), ListEvents()
        self.cn = ro.RoCorrespondent(CN, self.clock, SECRET, self.events, max_lifetime_s=40)
        self.mn = ro.RoMobile(HOME_ADDR, CN, self.clock, self.events, **mn_kw)

    def pump(self, outs):
        queue = list(outs)
        while queue:
            o = queue.pop(0)
            for rep in self.cn.handle_datagram(o.data, (str(o.src), 4350)):
                queue += self.mn.handle_datagram(rep.data, (CN, 4350))

    def register(self, coa="10.10.4.2"):
        self.pump(self.mn.start(coa))

    def advance(self, s, step=0.1):
        t = 0.0
        while t < s - 1e-9:
            self.clock.advance(step)
            t += step
            self.pump(self.mn.poll())
            self.cn.tick()


class ReturnRoutabilityTests(unittest.TestCase):
    def test_full_exchange_creates_a_binding(self):
        p = Pair()
        p.register("10.10.4.2")
        self.assertEqual(p.mn.state, "active")
        self.assertEqual(p.cn.lookup(HOME_ADDR), A("10.10.4.2"))
        kinds = [r["detail"].split()[0] for r in p.events.rows if r["kind"] in ("ro_signal_sent", "ro_signal_received") and r["node"] == "mn"]
        self.assertEqual(sorted(kinds), sorted(["CoTI", "HoTI", "CoT", "HoT", "BU", "BA"]))

    def test_tampered_binding_update_is_rejected(self):
        p = Pair()
        outs = p.mn.start("10.10.4.2")
        replies = [r for o in outs for r in p.cn.handle_datagram(o.data, (str(o.src), 4350))]
        bu = None
        for r in replies:
            bu = (p.mn.handle_datagram(r.data, (CN, 4350)) or [bu])[0]
        for i in (1, 2, 3, 4, 8, 10, 12, 16, 20, 30):                   # flip a bit anywhere in the BU body or MAC
            bad = bytearray(bu.data)
            bad[i] ^= 0x01
            out = p.cn.handle_datagram(bytes(bad), ("10.10.4.2", 4350))
            self.assertEqual(out[0].data[1], ro.ST_AUTH_FAILED, i)
        bad = bytearray(bu.data)
        bad[14] ^= 0x01                                                 # nonce index: refused as an unknown nonce
        self.assertEqual(p.cn.handle_datagram(bytes(bad), ("10.10.4.2", 4350))[0].data[1], ro.ST_BAD_NONCE)
        self.assertIsNone(p.cn.lookup(HOME_ADDR))

    def test_redirection_attack_without_the_care_of_token_fails(self):
        """Attacker holds a valid HOME token (e.g. sniffed) and a care-of token for ITS OWN address, and
        tries to point the victim's traffic at the victim's neighbour address."""
        p = Pair()
        attacker, victim_coa = A("10.10.9.9"), A("10.10.5.77")
        n = p.cn._nonce(p.cn._idx())
        home_tok = ro._token(SECRET, b"home", A(HOME_ADDR), n)                  # assume it leaked
        own_tok = ro._token(SECRET, b"coa", attacker, n)                          # obtainable by anyone for their own address
        kbm = ro.binding_key(home_tok, own_tok)
        bu = ro.encode_bu(kbm, p.cn.address, 1, 0, 40, A(HOME_ADDR), victim_coa, p.cn._idx())
        out = p.cn.handle_datagram(bu, (str(attacker), 4350))
        self.assertEqual(out[0].data[1], ro.ST_AUTH_FAILED)
        self.assertIsNone(p.cn.lookup(HOME_ADDR))
        self.assertIn("ro_rejected", p.events.kinds("cn"))

    def test_replayed_and_stale_sequence_numbers_are_rejected(self):
        p = Pair()
        outs = p.mn.start("10.10.4.2")
        captured = []
        orig = p.cn.handle_datagram
        p.cn.handle_datagram = lambda d, s: (captured.append(d) if d[0] == ro.T_BU else None) or orig(d, s)
        p.pump(outs)
        self.assertEqual(p.mn.state, "active")
        out = orig(captured[0], ("6.6.6.6", 4350))                                # exact replay from elsewhere
        self.assertEqual(out[0].data[1], ro.ST_SEQ_OUT_OF_WINDOW)
        self.assertEqual(p.cn.lookup(HOME_ADDR), A("10.10.4.2"))

    def test_expired_nonce_is_refused(self):
        p = Pair()
        outs = p.mn.start("10.10.4.2")
        replies = [r for o in outs for r in p.cn.handle_datagram(o.data, (str(o.src), 4350))]
        bus = [x for r in replies for x in p.mn.handle_datagram(r.data, (CN, 4350))]
        p.clock.advance(2 * 240 + 5)                                              # beyond current+previous nonce window
        out = p.cn.handle_datagram(bus[0].data, ("10.10.4.2", 4350))
        self.assertEqual(out[0].data[1], ro.ST_BAD_NONCE)

    def test_forged_tokens_and_forged_acknowledgements_are_ignored(self):
        p = Pair(retransmit_initial_s=0.2, max_retransmits=2)
        outs = p.mn.start("10.10.4.2")
        forged = ro._TOKEN.pack(ro.T_HOT, b"\x00" * 8, 0, b"\x11" * 8)             # wrong cookie
        self.assertEqual(p.mn.handle_datagram(forged, (CN, 4350)), [])
        p.pump(outs)
        self.assertEqual(p.mn.state, "active")
        # forged BA for the next BU never completes it: wrong MAC
        p.mn.start("10.10.4.3")
        for o in list(p.mn._last_out):
            for r in p.cn.handle_datagram(o.data, (str(o.src), 4350)):
                for bu in p.mn.handle_datagram(r.data, (CN, 4350)):
                    pass
        self.assertEqual(p.mn.state, "bu")
        fake = ro.encode_ba(b"k" * 32, A(CN), 0, p.mn._seq, 40)
        p.mn.handle_datagram(fake, (CN, 4350))
        self.assertEqual(p.mn.state, "bu")
        self.assertIn("ro_failed", p.events.kinds("mn"))

    def test_home_token_is_reused_until_it_expires(self):
        p = Pair()
        p.register("10.10.4.2")
        again = p.mn.start("10.10.5.2")
        self.assertEqual([ro.NAMES[o.data[0]] for o in again], ["CoTI"])           # no second home test
        p.pump(again)
        self.assertEqual(p.cn.lookup(HOME_ADDR), A("10.10.5.2"))
        p.clock.advance(215)
        self.assertEqual(sorted(ro.NAMES[o.data[0]] for o in p.mn.start("10.10.4.2")), ["CoTI", "HoTI"])

    def test_binding_expires_refreshes_and_deregisters(self):
        p = Pair(bu_lifetime_s=4, refresh_fraction=100.0)
        p.register()
        p.advance(3.5)
        self.assertIsNotNone(p.cn.lookup(HOME_ADDR))
        p.advance(1.0)
        self.assertIsNone(p.cn.lookup(HOME_ADDR))                                  # fell back: CN stops using the binding
        self.assertIn("ro_binding_removed", p.events.kinds("cn"))
        q = Pair(bu_lifetime_s=4)                                                  # refresh at 50 %
        q.register()
        q.advance(9.0)
        self.assertIsNotNone(q.cn.lookup(HOME_ADDR))
        self.assertGreaterEqual(q.mn._seq, 4)
        bu = ro.encode_bu(q.mn._kbm, q.cn.address, (q.mn._seq + 1) & 0xFFFF, ro.FLAG_DEREGISTER, 0, A(HOME_ADDR),
                          A("10.10.4.2"), q.mn._idx)
        q.cn.handle_datagram(bu, ("10.10.4.2", 4350))
        self.assertIsNone(q.cn.lookup(HOME_ADDR))

    def test_no_correspondent_means_failure_after_retries(self):
        clock, events = FakeClock(), ListEvents()
        mn = ro.RoMobile(HOME_ADDR, CN, clock, events, retransmit_initial_s=0.1, max_retransmits=2)
        mn.start("10.10.4.2")                                                      # nobody answers, ever
        sent = 0
        for _ in range(40):
            clock.advance(0.1)
            sent += len(mn.poll())
        self.assertEqual(mn.state, "failed")
        self.assertEqual(sent, 4)                                                  # 2 retransmissions x (CoTI + HoTI)
        self.assertIn("ro_failed", events.kinds("mn"))


class RoOnTheTestbed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def sim(self, customise=None, run_id="r", **kw):
        spec = MobilitySpec(topology=DEFAULT_TOPOLOGY, run_id=run_id, **kw)
        s = RoSim(spec, self.tmp.name, ro_lifetime_s=kw.pop("ro_lifetime_s", 40)) if False else RoSim(spec, self.tmp.name)
        if customise:
            customise(s)
        run = s.run()
        return s, analyze_run(run)

    def test_data_bypasses_the_home_agent_once_the_binding_is_active(self):
        s, m = self.sim(duration_s=6, moves=())
        self.assertGreater(s.ro_stats["packets"], 450)
        self.assertLess(s.ha.stats["tunnel_packets"], 60)           # the HA tunnel only carried the first probes and home tokens
        self.assertEqual((s.ro_stats["outer_bytes"] - s.ro_stats["inner_bytes"]) / s.ro_stats["packets"], 20.0)
        self.assertEqual(m["loss"]["lost"], 0)

    def test_steady_state_rtt_is_lower_than_via_the_home_agent_but_handoff_is_worse(self):
        base = ExperimentSpec("mip-basic")
        basic = run_experiment(replace(base, scenario_id="b"), self.tmp.name).metrics
        opt = run_experiment(replace(base, approach="ro-model", scenario_id="r"), self.tmp.name).metrics
        self.assertEqual(basic["handoffs"][0]["rtt_before_ms"], 33.0)          # forward via HA 20 ms + direct return 13 ms
        self.assertEqual(opt["handoffs"][0]["rtt_before_ms"], 26.0)            # direct both ways
        self.assertGreater(opt["handoff_summary"]["handoff_latency_ms_mean"], basic["handoff_summary"]["handoff_latency_ms_mean"])
        self.assertGreater(opt["loss"]["lost"], basic["loss"]["lost"])         # stale care-of address used until the BU is accepted
        self.assertGreater(opt["handoff_summary"]["signalling_messages_total"], basic["handoff_summary"]["signalling_messages_total"])
        self.assertTrue(opt["continuity"]["all_handoffs_recovered"])

    def test_ro_binding_expiry_falls_back_to_the_home_agent_without_loss(self):
        def never_refresh(s):
            s.ro_mn.refresh_fraction = 100.0
        s, m = RoSim(MobilitySpec(topology=DEFAULT_TOPOLOGY, run_id="e", duration_s=6, moves=(), deregister_at_end=False),
                     self.tmp.name, ro_lifetime_s=2), None
        never_refresh(s)
        m = analyze_run(s.run())
        self.assertEqual(m["loss"]["lost"], 0)
        self.assertTrue(100 < s.ro_stats["packets"] < 260)                         # ~2 s of the 5.5 s run went direct
        self.assertGreater(s.ha.stats["tunnel_packets"], 300)                      # the rest again through the home agent

    def test_correspondent_without_ro_support_degrades_gracefully(self):
        def no_ro(s):
            s.cn.udp.pop(4350)
            s.ro_mn.rt_initial = int(0.1e9)
        s, m = self.sim(customise=no_ro, run_id="n", duration_s=5, moves=())
        self.assertEqual(s.ro_mn.state, "failed")
        self.assertEqual(s.ro_stats["packets"], 0)
        self.assertEqual(m["loss"]["lost"], 0)                                    # traffic simply keeps using the home agent


if __name__ == "__main__":
    unittest.main()
