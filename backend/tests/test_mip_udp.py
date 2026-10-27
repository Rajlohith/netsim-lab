"""Registration over REAL UDP sockets on loopback (127.0.0.0/8 addresses stand in for the lab addresses)."""
import ipaddress
import socket
import threading
import time
import unittest

from app.mobileip import messages as M
from app.mobileip.dataplane import RecordingHaDataPlane, RecordingMnDataPlane
from app.mobileip.events import NullEvents
from app.mobileip.home_agent import HomeAgent
from app.mobileip.mobile_node import MobileNode
from app.mobileip.runtime import HomeAgentServer, MobileNodeClient, RealClock
from app.traffic.shutdown import ShutdownFlag
from tests.mip_helpers import ListEvents

A = ipaddress.IPv4Address
AGENT, HOME, NET = "127.0.0.1", "127.9.0.2", "127.9.0.0/24"
COA1, COA2 = "127.1.0.2", "127.2.0.2"
KEY = b"loopback-test-key-0123456789"


class UdpBase(unittest.TestCase):
    def setUp(self):
        self.ha_events, self.mn_events = ListEvents(), ListEvents()
        self.ha_dp, self.mn_dp = RecordingHaDataPlane(), RecordingMnDataPlane()
        self.ha = HomeAgent(AGENT, NET, {HOME: M.SecurityAssociation(256, KEY)}, self.ha_dp, RealClock,
                            self.ha_events, max_lifetime_s=300)
        self.flag = ShutdownFlag()
        self.server = HomeAgentServer(self.ha, (AGENT, 0), self.flag, tick_s=0.02)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(500):
            if self.server.port:
                break
            time.sleep(0.01)
        self.port = self.server.port
        self.addCleanup(self._stop)
        self.clients = []

    def _stop(self):
        for c in self.clients:
            c.close()
        self.flag.request("test")
        self.thread.join(timeout=5)

    def client(self, key=KEY, lifetime=60, **kw):
        mn = MobileNode(HOME, AGENT, M.SecurityAssociation(256, key), RealClock, self.mn_dp, self.mn_events,
                        requested_lifetime_s=lifetime, agent_port=self.port, **kw)
        c = MobileNodeClient(mn)
        self.clients.append(c)
        return c


class UdpRegistrationTests(UdpBase):
    def test_register_over_real_sockets(self):
        c = self.client()
        c.attach(COA1)
        self.assertTrue(c.wait_state({"registered"}, 3))
        self.assertEqual(str(self.ha.bindings.get(HOME).care_of_address), COA1)
        self.assertEqual(self.ha_dp.tunnels, {A(HOME): A(COA1)})
        self.assertEqual(self.mn_dp.care_of, A(COA1))
        rtt = [r for r in self.mn_events.rows if r["kind"] == "reg_reply_received"][0]["detail"]
        self.assertIn("rtt_ns=", rtt)

    def test_binding_update_moves_the_tunnel(self):
        c = self.client()
        c.attach(COA1)
        self.assertTrue(c.wait_state({"registered"}, 3))
        c.attach(COA2)                                              # new socket bound to the new care-of address
        self.assertTrue(c.wait_state({"registered"}, 3))
        self.assertEqual(c.core.care_of, A(COA2))
        deadline = time.time() + 2
        while time.time() < deadline and self.ha_dp.tunnels.get(A(HOME)) != A(COA2):
            time.sleep(0.01)
        self.assertEqual(self.ha_dp.tunnels, {A(HOME): A(COA2)})
        self.assertEqual(self.ha.bindings.get(HOME).registrations, 2)
        self.assertEqual(self.ha.bindings.get(HOME).care_of_address, A(COA2))

    def test_reply_goes_to_the_source_address_of_the_request(self):
        c = self.client()
        c.attach(COA2)
        self.assertTrue(c.wait_state({"registered"}, 3))
        src = [r for r in self.ha_events.rows if r["kind"] == "reg_request_received"][0]["detail"]
        self.assertTrue(src.startswith(f"from {COA2}:"))

    def test_deregistration_removes_binding(self):
        c = self.client()
        c.attach(COA1)
        c.wait_state({"registered"}, 3)
        c.deregister()
        self.assertTrue(c.wait_state({"detached"}, 3))
        self.assertEqual(len(self.ha.bindings), 0)
        self.assertEqual(self.ha_dp.tunnels, {})
        self.assertIsNone(self.mn_dp.care_of)

    def test_real_time_expiry(self):
        self.ha.max_lifetime_s = 1
        c = self.client(lifetime=600, reregister_fraction=100.0)
        c.attach(COA1)
        c.wait_state({"registered"}, 3)
        self.assertEqual(c.core.granted_lifetime_s, 1)              # HA shortened it
        time.sleep(1.4)
        self.assertEqual(len(self.ha.bindings), 0)
        self.assertEqual(self.ha_dp.tunnels, {})
        self.assertIn("binding_expired", self.ha_events.kinds("ha"))

    def test_renewal_keeps_binding(self):
        self.ha.max_lifetime_s = 1
        c = self.client(lifetime=600)                                # renews at half of the granted 1 s
        c.attach(COA1)
        c.wait_state({"registered"}, 3)
        end = time.time() + 2.5
        while time.time() < end:
            c.step(0.02)
        self.assertEqual(len(self.ha.bindings), 1)
        self.assertGreaterEqual(self.ha.bindings.get(HOME).registrations, 3)
        self.assertEqual(self.ha_events.kinds("ha").count("binding_expired"), 0)

    def test_wrong_key_never_registers(self):
        c = self.client(key=b"definitely-the-wrong-key!!", retransmit_initial_s=0.1, max_retransmits=2)
        c.attach(COA1)
        self.assertTrue(c.wait_state({"failed"}, 5))
        self.assertEqual(len(self.ha.bindings), 0)
        self.assertEqual(self.ha_dp.calls, [])
        self.assertGreaterEqual(self.ha_events.kinds("ha").count("auth_failure"), 1)

    def test_replayed_datagram_rejected_by_live_server(self):
        c = self.client()
        c.attach(COA1)
        c.wait_state({"registered"}, 3)
        # an attacker re-sends an old authenticated request from another source address
        sa = M.SecurityAssociation(256, KEY)
        old = M.RegistrationRequest(M.FLAG_D, 600, A(HOME), A(AGENT), A("127.66.0.1"),
                                    M.ident_from_wall_ns(RealClock.wall_ns() - 1_000_000_000))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind(("127.66.0.1", 0))
            s.settimeout(2)
            s.sendto(M.encode_request(old, sa), (AGENT, self.port))
            rep = M.decode_reply(s.recvfrom(2048)[0])
        self.assertEqual(rep.code, M.CODE_HA_IDENT_MISMATCH)        # older than the last accepted identification
        self.assertEqual(self.ha.bindings.get(HOME).care_of_address, A(COA1))

    def test_garbage_datagrams_do_not_disturb_the_server(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for junk in (b"", b"\x01", b"\xff" * 100, b"\x01" * 24):
                s.sendto(junk, (AGENT, self.port))
        c = self.client()
        c.attach(COA1)
        self.assertTrue(c.wait_state({"registered"}, 3))

    def test_server_shutdown_cleans_up_bindings(self):
        c = self.client()
        c.attach(COA1)
        c.wait_state({"registered"}, 3)
        self.flag.request("test")
        self.thread.join(timeout=5)
        self.assertEqual(len(self.ha.bindings), 0)
        self.assertEqual(self.ha_dp.tunnels, {})
        self.assertIn("tunnel_stats", self.ha_events.kinds("ha"))


if __name__ == "__main__":
    unittest.main()
