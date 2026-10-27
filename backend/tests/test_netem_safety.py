import unittest
from pathlib import Path

from app.netem import plan
from app.netem.errors import SafetyError
from app.netem.safety import validate_argv
from app.netem.topology_config import NetemSpec, load_config

CFG = Path(__file__).resolve().parents[2] / "config" / "topology.yaml"
P = "nsl-"


class SafetyTests(unittest.TestCase):
    def test_every_planned_command_is_allowed(self):
        cfg = load_config(CFG)
        cmds = plan.build_create_plan(cfg, P)
        cmds += [plan.ns_del("nsl-cn"), plan.ns_list(), plan.ping_cmd("nsl-mn", "10.10.1.2"),
                 plan.shape_cmd("nsl-ha", "eth-ana", NetemSpec(delay_ms=30, loss_percent=5)),
                 plan.shape_cmd("nsl-ha", "eth-ana", None)]
        cmds += plan.status_cmds("nsl-ha")
        for c in cmds:
            validate_argv(c.argv, P)

    def test_forbidden_commands_rejected(self):
        bad = [
            ["iptables", "-F"],
            ["nft", "flush", "ruleset"],
            ["firewall-cmd", "--reload"],
            ["ip", "route", "add", "default", "via", "10.0.0.1"],           # host default route
            ["ip", "route", "replace", "default", "via", "10.0.0.1"],
            ["ip", "link", "set", "eth0", "down"],                          # host interface
            ["ip", "netns", "del", "default"],
            ["ip", "netns", "del", "docker"],
            ["ip", "netns", "del", "nsl-"],                                  # empty key
            ["ip", "netns", "del", "nsl-CN"],
            ["ip", "netns", "del", "nsl-cn; rm -rf /"],
            ["ip", "netns", "del", "../nsl-cn"],
            ["ip", "-n", "othernet", "route", "add", "default", "via", "10.0.0.1"],
            ["ip", "-n", "nsl-cn", "route", "flush", "all"],
            ["ip", "netns", "exec", "nsl-cn", "iptables", "-F"],
            ["ip", "netns", "exec", "nsl-cn", "sh", "-c", "id"],
            ["ip", "netns", "exec", "nsl-cn", "sysctl", "-w", "net.ipv4.ip_forward=1;id"],
            ["ip", "netns", "exec", "nsl-cn", "sysctl", "-w", "kernel.core_pattern=x"],
            ["ip", "netns", "exec", "nsl-cn", "tc", "qdisc", "replace", "dev", "eth0", "root", "netem", "delay", "1ms;id"],
            ["ip", "netns", "exec", "nsl-cn", "ping", "-n", "-c", "1", "-i", "0.2", "-W", "1", "8.8.8.8 -f"],
            ["ip", "-n", "nsl-cn", "link", "add", "x", "type", "veth", "peer", "name", "y", "netns", "default"],
            ["sudo", "ip", "netns", "add", "nsl-cn"],
            [],
            ["ip", "netns", "add", ""],
        ]
        for argv in bad:
            with self.subTest(argv=argv), self.assertRaises(SafetyError):
                validate_argv(argv, P)

    def test_custom_prefix_is_respected(self):
        validate_argv(["ip", "netns", "add", "nslt-cn"], "nslt-")
        with self.assertRaises(SafetyError):
            validate_argv(["ip", "netns", "add", "nsl-cn"], "nslt-")

    def test_all_routes_and_links_are_namespaced(self):
        for c in plan.build_create_plan(load_config(CFG), P):
            a = c.argv
            if a[1:3] == ("netns", "add"):
                continue
            self.assertTrue(a[1] == "-n" or a[1:3] == ("netns", "exec"), a)
            self.assertNotIn("iptables", a)
            self.assertNotIn("nft", a)


if __name__ == "__main__":
    unittest.main()
