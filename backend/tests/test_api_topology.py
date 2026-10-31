"""GET /api/topology (service layer): the view must be exactly what the Stage 2 loader parses from the file the experiments use."""
import unittest

import yaml

from app.compare.experiment import DEFAULT_TOPOLOGY
from app.netem.topology_config import load_config
from app.services import errors as E
from app.services.topology import topology_view


class TopologyView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = topology_view()
        cls.raw = yaml.safe_load(DEFAULT_TOPOLOGY.read_text())
        cls.cfg = load_config(DEFAULT_TOPOLOGY)

    def test_nodes_links_routes_match_the_yaml(self):
        self.assertEqual(self.v["name"], self.raw["name"])
        self.assertEqual({n["key"]: n["role"] for n in self.v["nodes"]}, {k: n["role"] for k, n in self.raw["nodes"].items()})
        self.assertEqual([l["name"] for l in self.v["links"]], [l["name"] for l in self.raw["links"]])
        self.assertEqual(len(self.v["routes"]), len(self.raw["routes"]))

    def test_link_netem_is_the_configured_value(self):
        by = {l["name"]: l for l in self.v["links"]}
        for raw in self.raw["links"]:
            want = raw.get("netem", {})
            for side in ("a", "b"):
                got = by[raw["name"]][side]["netem"]
                self.assertEqual(got["delay_ms"], want.get("delay_ms", 0.0))
                self.assertEqual(got["jitter_ms"], want.get("jitter_ms", 0.0))
                self.assertEqual(got["rate_mbit"], want.get("rate_mbit"))

    def test_interfaces_reference_existing_links(self):
        names = {l["name"] for l in self.v["links"]}
        for n in self.v["nodes"]:
            for i in n["interfaces"]:
                self.assertIn(i["link"], names)

    def test_json_serialisable_and_source_is_relative(self):
        import json
        json.dumps(self.v)
        self.assertEqual(self.v["source_file"], "config/topology_ro.yaml")

    def test_unreadable_file_is_a_configuration_error(self):
        with self.assertRaises(E.ConfigurationError):
            topology_view("/nonexistent/topology.yaml")


if __name__ == "__main__":
    unittest.main()
