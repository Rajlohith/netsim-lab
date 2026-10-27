"""The common experiment interface: every supported approach runs through it and yields the same kind of record;
everything else is reported explicitly."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.compare import approaches, matrix
from app.compare.experiment import ExperimentSpec
from app.compare.quic_sim import quic_available
from app.compare.runner import check, run_experiment

RUNNABLE = ("tcp-reconnect", "mip-basic", "mip-mbb", "ro-model")
FILES = ("sent.csv", "received.csv", "echoes.csv", "events.csv", "approach_events.csv", "sender.meta.json",
         "receiver.meta.json", "scenario.json", "experiment_summary.json", "experiment.json", "metrics.json",
         "comparison_metrics.json")


class CommonInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.results = {a: run_experiment(ExperimentSpec(a), cls.tmp.name) for a in RUNNABLE}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_each_supported_approach_runs_and_writes_the_same_files(self):
        for a, r in self.results.items():
            with self.subTest(approach=a):
                self.assertEqual((r.status, r.reason), ("ok", ""))
                for f in FILES:
                    self.assertTrue((r.run_dir / f).exists(), f)

    def test_metrics_have_identical_structure_for_every_approach(self):
        keys = {a: set(r.metrics) for a, r in self.results.items()}
        hkeys = {a: set(r.metrics["handoffs"][0]) for a, r in self.results.items()}
        ref = next(iter(keys.values()))
        for a in RUNNABLE:
            self.assertEqual(keys[a], ref, a)
            self.assertEqual(hkeys[a], hkeys["tcp-reconnect"], a)
            self.assertEqual(set(self.results[a].metrics["continuity"]) >= {"all_handoffs_recovered", "data_loss_free",
                                                                             "endpoint_address_preserved"}, True)

    def test_handoff_events_are_recorded_consistently(self):
        for a, r in self.results.items():
            with self.subTest(approach=a):
                self.assertEqual([h["event_id"] for h in r.metrics["handoffs"]], ["handoff-1", "handoff-2"])
                self.assertTrue(all(h["recovered"] for h in r.metrics["handoffs"]))
                self.assertTrue(all(h["handoff_latency_ms"] > 0 for h in r.metrics["handoffs"]))
                self.assertTrue(all(h["service_gap_ms"] is not None for h in r.metrics["handoffs"]))
                self.assertTrue(r.metrics["loss"]["exact"])
                ev = json.loads((r.run_dir / "sender.meta.json").read_text())
                self.assertEqual((ev["scenario_id"], ev["run_id"]), (r.spec.scenario_id, r.spec.run_id))

    def test_results_agree_with_independently_known_values(self):
        m = {a: r.metrics["handoff_summary"] for a, r in self.results.items()}
        self.assertEqual(m["mip-basic"]["handoff_latency_ms_mean"], 71.0)      # registration RTT via B (71) and A (31) averaged with detection
        self.assertEqual(self.results["mip-basic"].metrics["loss"]["lost"], 16)
        self.assertEqual(self.results["mip-mbb"].metrics["loss"]["lost"], 0)
        self.assertEqual(self.results["tcp-reconnect"].metrics["loss"]["lost"], 0)
        self.assertEqual(self.results["tcp-reconnect"].metrics["mobility_overhead"]["mobility_bytes_per_packet"], 0.0)
        for a in ("mip-basic", "mip-mbb", "ro-model"):
            self.assertEqual(self.results[a].metrics["mobility_overhead"]["mobility_bytes_per_packet"], 20.0)

    def test_continuity_flags_distinguish_the_approaches(self):
        c = {a: r.metrics["continuity"] for a, r in self.results.items()}
        self.assertFalse(c["tcp-reconnect"]["transport_connection_preserved"])
        self.assertEqual(c["tcp-reconnect"]["connections_opened"], 3)
        self.assertFalse(c["tcp-reconnect"]["endpoint_address_preserved"])
        for a in ("mip-basic", "mip-mbb", "ro-model"):
            self.assertTrue(c[a]["endpoint_address_preserved"])
            self.assertIsNone(c[a]["transport_connection_preserved"])          # UDP probes: there is no connection to preserve

    def test_effective_handoff_mode_is_recorded(self):
        e = json.loads((self.results["mip-mbb"].run_dir / "experiment.json").read_text())
        self.assertEqual(e["effective_handoff_mode"], "mbb")
        e = json.loads((self.results["mip-basic"].run_dir / "experiment.json").read_text())
        self.assertEqual(e["effective_handoff_mode"], "bbm")
        self.assertIn("NOT RFC 5944", e["approach"]["status"])

    def test_ro_model_is_labelled_as_a_simplified_model_of_mobile_ipv6(self):
        info = approaches.APPROACHES["ro-model"]
        self.assertIn("Mobile IPv6", info.status)
        self.assertIn("NOT Mobile IPv6", info.status)
        self.assertIn("not Mobile IPv4", info.status)

    def test_quic_scope_statement_does_not_claim_tcp_support(self):
        info = approaches.APPROACHES["quic-migration"]
        self.assertIn("does NOT make arbitrary TCP applications mobile", info.scope_limits)

    def test_repeatable_metrics(self):
        again = run_experiment(ExperimentSpec("tcp-reconnect", run_id="again"), self.tmp.name)
        a, b = dict(self.results["tcp-reconnect"].metrics), dict(again.metrics)
        a.pop("run_id"), b.pop("run_id")
        self.assertEqual(a, b)


class ExplicitReporting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_quic_availability_is_reported_not_assumed(self):
        r = run_experiment(ExperimentSpec("quic-migration"), self.tmp.name)
        if quic_available():
            self.assertIn(r.status, ("ok", "failed"))
        else:
            self.assertEqual(r.status, "unavailable")
            self.assertIn("aioquic", r.reason)
            self.assertIsNone(r.metrics)
            e = json.loads((r.run_dir / "experiment.json").read_text())
            self.assertEqual(e["status"], "unavailable")

    def test_every_unsupported_combination_has_a_reason_and_a_record(self):
        for s in matrix.probe_specs():
            r = run_experiment(s, self.tmp.name)
            with self.subTest(scenario=s.scenario_id):
                self.assertIn(r.status, ("unsupported", "unavailable"))
                self.assertTrue(len(r.reason) > 30, r.reason)
                self.assertIsNone(r.metrics)
                self.assertFalse((r.run_dir / "sent.csv").exists())              # nothing was half-run
                self.assertEqual(json.loads((r.run_dir / "experiment.json").read_text())["reason"], r.reason)

    def test_unknown_inputs_are_rejected_with_the_known_values(self):
        for spec, text in ((ExperimentSpec("nope"), "known"), (ExperimentSpec("tcp-reconnect", environment="mars"), "known"),
                           (ExperimentSpec("tcp-reconnect", handoff_mode="sideways"), "known")):
            st, why = check(spec)
            self.assertEqual(st, "unsupported")
            self.assertIn(text, why)

    def test_a_crashing_adapter_is_reported_as_failed_with_a_traceback(self):
        from unittest import mock
        with mock.patch("app.compare.runner._dispatch", side_effect=RuntimeError("boom")):
            r = run_experiment(ExperimentSpec("tcp-reconnect"), self.tmp.name)
        self.assertEqual((r.status, r.reason), ("failed", "RuntimeError: boom"))
        self.assertIn("boom", (r.run_dir / "traceback.txt").read_text())
        self.assertEqual(json.loads((r.run_dir / "experiment.json").read_text())["status"], "failed")

    def test_unreadable_topology_is_an_invalid_parameter_not_a_crash(self):
        r = run_experiment(ExperimentSpec("tcp-reconnect", topology=Path("/nonexistent/topology.yaml")), self.tmp.name)
        self.assertEqual(r.status, "invalid")
        self.assertIn("cannot use topology", r.reason)

    def test_live_environment_without_a_lab_is_a_failure_not_a_silent_skip(self):
        r = run_experiment(ExperimentSpec("mip-basic", environment="live"), self.tmp.name)
        self.assertEqual(r.status, "failed")
        self.assertIn("LiveContext", r.reason)

    def test_support_table_covers_every_approach_and_environment(self):
        for a in approaches.APPROACHES:
            self.assertEqual(set(approaches.SUPPORT[a]), {"emulated", "loopback", "live"})
            for env, (st, why) in approaches.SUPPORT[a].items():
                self.assertIn(st, ("supported", "unverified", "unsupported"))
                self.assertTrue(why)


class MatrixTests(unittest.TestCase):
    def test_report_contains_every_approach_every_reason_and_the_unverified_markers(self):
        with tempfile.TemporaryDirectory() as d:
            results, path = matrix.run_matrix(d, ExperimentSpec("tcp-reconnect", duration_s=10))
            md = Path(path).read_text()
            for a in approaches.APPROACHES:
                self.assertIn(f"`{a}`", md)
            for r in results:
                if r.status != "ok":
                    self.assertIn(r.reason, md)
            self.assertIn("UNVERIFIED", md)
            self.assertIn("NOT Mobile IPv6", md)
            data = json.loads((Path(d) / "comparison.json").read_text())
            self.assertEqual(len(data), len(results))
            self.assertEqual({x["status"] for x in data} <= {"ok", "unsupported", "unavailable", "failed"}, True)
            ok = [x for x in data if x["status"] == "ok"]
            self.assertGreaterEqual(len(ok), 5)                                  # tcp x2, mip-basic, mip-mbb, ro-model
            self.assertNotIn("failed", {x["status"] for x in data})


if __name__ == "__main__":
    unittest.main()
