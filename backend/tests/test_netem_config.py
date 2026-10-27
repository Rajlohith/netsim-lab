import copy
import unittest
from pathlib import Path

import yaml

from app.netem.errors import ConfigError
from app.netem.topology_config import NetemSpec, ROLES, load_config, parse_config

CFG = Path(__file__).resolve().parents[2] / "config" / "topology.yaml"


def raw():
    return copy.deepcopy(yaml.safe_load(CFG.read_text()))


class ConfigTests(unittest.TestCase):
    def test_default_config_loads(self):
        cfg = load_config(CFG)
        self.assertEqual(len(cfg.nodes), 5)
        self.assertEqual({n.role for n in cfg.nodes.values()}, set(ROLES))
        self.assertEqual(len(cfg.links), 5)
        self.assertTrue(cfg.node_by_role("home-agent").forwarding)
        self.assertFalse(cfg.node_by_role("mobile-node").forwarding)

    def test_effective_netem_end_overrides_link(self):
        d = raw()
        d["links"][0]["a"]["netem"] = {"loss_percent": 2}
        cfg = parse_config(d)
        link = cfg.links[0]
        self.assertEqual(link.effective_netem(link.a).loss_percent, 2)
        self.assertEqual(link.effective_netem(link.b).delay_ms, 5)

    def _bad(self, mutate, text):
        d = raw()
        mutate(d)
        with self.assertRaises(ConfigError) as cm:
            parse_config(d)
        self.assertIn(text, str(cm.exception))

    def test_rejections(self):
        self._bad(lambda d: d.update(version=2), "version")
        self._bad(lambda d: d["nodes"].update({"Bad_Key": d["nodes"].pop("cn")}), "invalid node key")
        self._bad(lambda d: d["nodes"]["cn"].update(role="home-agent"), "exactly once")
        self._bad(lambda d: d["links"][0]["a"].update(ifname="this-name-is-way-too-long"), "interface name")
        self._bad(lambda d: d["links"][0]["a"].update(address="10.10.9.2/24"), "same subnet")
        self._bad(lambda d: d["links"][1]["a"].update(address="10.10.1.9/24"), "same subnet")
        self._bad(lambda d: d["routes"][0].update(via="192.168.99.1"), "directly connected")
        self._bad(lambda d: d["routes"][0].update(to="10.0.0.1/8"), "bad destination")
        self._bad(lambda d: d["links"][0].update(netem={"loss_percent": 101}), "outside allowed range")
        self._bad(lambda d: d["links"][0].update(netem={"jitter_ms": 5}), "jitter_ms requires")
        self._bad(lambda d: d["links"][0].update(netem={"bogus": 1}), "unknown field")
        self._bad(lambda d: d["links"][0].update(name="cn-ha") or d["links"][1].update(name="cn-ha"), "duplicate link")

    def test_overlapping_subnets_rejected(self):
        d = raw()
        for end, ip in (("a", "10.10.1.5/24"), ("b", "10.10.1.6/24")):
            d["links"][1][end]["address"] = ip
        d["routes"] = []
        with self.assertRaises(ConfigError):
            parse_config(d)

    def test_netem_args(self):
        self.assertEqual(NetemSpec(delay_ms=50, jitter_ms=5, loss_percent=1.5, rate_mbit=10).to_args(),
                         ["delay", "50ms", "5ms", "loss", "1.5%", "rate", "10mbit"])
        self.assertTrue(NetemSpec().is_empty())
        self.assertEqual(NetemSpec().to_args(), [])


if __name__ == "__main__":
    unittest.main()
