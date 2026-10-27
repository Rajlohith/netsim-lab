import unittest
from pathlib import Path

from app.netem import plan
from app.netem.topology_config import load_config

CFG = Path(__file__).resolve().parents[2] / "config" / "topology.yaml"


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.cmds = [tuple(c.argv) for c in plan.build_create_plan(load_config(CFG), "nsl-")]

    def has(self, *argv):
        return tuple(argv) in self.cmds

    def test_namespaces_created_first_for_all_five_nodes(self):
        adds = [c[3] for c in self.cmds if c[1:3] == ("netns", "add")]
        self.assertEqual(sorted(adds), ["nsl-ana", "nsl-anb", "nsl-cn", "nsl-ha", "nsl-mn"])

    def test_veth_created_inside_namespaces(self):
        self.assertTrue(self.has("ip", "-n", "nsl-cn", "link", "add", "eth-ha", "type", "veth",
                                 "peer", "name", "eth-cn", "netns", "nsl-ha"))
        self.assertEqual(sum(1 for c in self.cmds if "veth" in c), 5)

    def test_addresses_and_up(self):
        self.assertTrue(self.has("ip", "-n", "nsl-mn", "addr", "add", "10.10.4.2/24", "dev", "eth-a"))
        self.assertTrue(self.has("ip", "-n", "nsl-mn", "link", "set", "eth-b", "up"))

    def test_forwarding_only_on_router_nodes(self):
        on = {c[3] for c in self.cmds if c[4:] == ("sysctl", "-w", "net.ipv4.ip_forward=1")}
        off = {c[3] for c in self.cmds if c[4:] == ("sysctl", "-w", "net.ipv4.ip_forward=0")}
        self.assertEqual(on, {"nsl-ha", "nsl-ana", "nsl-anb"})
        self.assertEqual(off, {"nsl-cn", "nsl-mn"})

    def test_mn_has_primary_and_standby_default_routes(self):
        self.assertTrue(self.has("ip", "-n", "nsl-mn", "route", "add", "default", "via", "10.10.4.1", "metric", "100"))
        self.assertTrue(self.has("ip", "-n", "nsl-mn", "route", "add", "default", "via", "10.10.5.1", "metric", "200"))

    def test_netem_commands(self):
        self.assertTrue(self.has("ip", "netns", "exec", "nsl-ha", "tc", "qdisc", "add", "dev", "eth-ana",
                                 "root", "netem", "delay", "10ms"))
        self.assertTrue(self.has("ip", "netns", "exec", "nsl-mn", "tc", "qdisc", "add", "dev", "eth-b",
                                 "root", "netem", "delay", "15ms", "2ms", "rate", "10mbit"))

    def test_ordering_netns_before_links_before_routes(self):
        idx = lambda pred: [i for i, c in enumerate(self.cmds) if pred(c)]
        last_ns = max(idx(lambda c: c[1:3] == ("netns", "add")))
        first_link = min(idx(lambda c: "veth" in c))
        first_route = min(idx(lambda c: "route" in c))
        first_tc = min(idx(lambda c: "tc" in c))
        self.assertLess(last_ns, first_link)
        self.assertLess(first_link, first_route)
        self.assertLess(first_route, first_tc)


if __name__ == "__main__":
    unittest.main()
