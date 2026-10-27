"""Regression tests for problems found by the Stage 5 verification sweep: results that said 'ok' but were meaningless."""
import json
import tempfile
import unittest
from dataclasses import replace

from app.compare.experiment import ExperimentSpec
from app.compare.runner import MAX_TUNNEL_PAYLOAD, check, run_experiment, validate


class InvalidParameters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_each_bad_parameter_is_rejected_with_its_reason_before_anything_runs(self):
        cases = {
            "duration_s must be": dict(duration_s=0), "rate_pps must be": dict(rate_pps=-5), "payload must be": dict(payload=20),
            "payload must be ": dict(payload=1473), "detect_delay_ms must be": dict(detect_delay_ms=-1.0),
            "probe_start_s must be smaller": dict(probe_start_s=10.0), "tcp_detect must be": dict(tcp_detect="sometimes"),
            "link_semantics must be": dict(link_semantics="magic"), "unknown interface": dict(moves=((2.0, "eth-z"),)),
            "initial_iface": dict(initial_iface="eth-z"), "increasing time order": dict(moves=((4.0, "eth-b"), (2.0, "eth-a"))),
            "already on": dict(moves=((2.0, "eth-a"),)), "outside the run": dict(moves=((10.0, "eth-b"),)),
            "must be (seconds, interface)": dict(moves=("eth-b",)),
        }
        for text, kw in cases.items():
            with self.subTest(text=text):
                st, why = check(ExperimentSpec("tcp-reconnect", **kw))
                self.assertEqual(st, "invalid")
                self.assertIn(text, why)

    def test_invalid_runs_leave_an_explicit_record_and_no_data(self):
        r = run_experiment(ExperimentSpec("tcp-reconnect", duration_s=0), self.tmp.name)
        self.assertEqual(r.status, "invalid")
        self.assertIsNone(r.metrics)
        self.assertFalse((r.run_dir / "sent.csv").exists())
        e = json.loads((r.run_dir / "experiment.json").read_text())
        self.assertEqual((e["status"], e["reason"]), ("invalid", r.reason))

    def test_valid_edge_values_are_accepted(self):
        for kw in (dict(payload=36), dict(payload=1472), dict(rate_pps=5000), dict(moves=()), dict(moves=((0.0, "eth-b"),)),
                   dict(moves=((9.999, "eth-b"),)), dict(detect_delay_ms=0.0), dict(old_link_hold_ms=0.0)):
            self.assertIsNone(validate(ExperimentSpec("tcp-reconnect", **kw)), kw)


class Portability(unittest.TestCase):
    def test_recorded_specs_contain_no_absolute_paths_of_the_producing_machine(self):
        """Found by the pre-export scan: experiment.json embedded '/home/<user>/.../config/topology_ro.yaml'."""
        j = ExperimentSpec("tcp-reconnect").to_json()
        self.assertEqual(j["topology"], "config/topology_ro.yaml")
        with tempfile.TemporaryDirectory() as d:
            r = run_experiment(ExperimentSpec("tcp-reconnect", duration_s=3, moves=((1.5, "eth-b"),)), d)
            self.assertNotIn("/home/", (r.run_dir / "experiment.json").read_text())
            self.assertNotIn(d, (r.run_dir / "experiment.json").read_text())


class TunnelMtu(unittest.TestCase):
    """Found by the sweep: payload 1472 + encapsulation exceeds the MTU; every packet was dropped but the status was 'ok'."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_boundary_is_exact(self):
        self.assertEqual(MAX_TUNNEL_PAYLOAD, 1452)
        for a in ("mip-basic", "mip-mbb", "ro-model"):
            self.assertEqual(check(ExperimentSpec(a, payload=1452))[0], "ok", a)
            st, why = check(ExperimentSpec(a, payload=1453))
            self.assertEqual(st, "unsupported", a)
            self.assertIn("MTU", why)
            self.assertIn("path-MTU", why)

    def test_the_largest_allowed_payload_really_is_delivered_through_the_tunnel(self):
        r = run_experiment(ExperimentSpec("mip-basic", payload=1452, duration_s=6, moves=((3.0, "eth-b"),)), self.tmp.name)
        self.assertEqual(r.status, "ok")
        self.assertLess(r.metrics["loss"]["lost"], 20)               # only the handoff loss, not "everything"
        self.assertEqual(r.metrics["mobility_overhead"]["mobility_bytes_per_packet"], 20.0)

    def test_transports_that_segment_are_not_limited(self):
        r = run_experiment(ExperimentSpec("tcp-reconnect", payload=1472, duration_s=6, moves=((3.0, "eth-b"),)), self.tmp.name)
        self.assertEqual((r.status, r.metrics["loss"]["lost"]), ("ok", 0))


if __name__ == "__main__":
    unittest.main()
