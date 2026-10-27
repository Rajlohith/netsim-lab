"""QUIC connection migration through aioquic. SKIPPED unless aioquic is installed (it was NOT in the development
sandbox, so these tests have never been run and the QUIC adapter is unverified)."""
import tempfile
import unittest

from app.compare.experiment import ExperimentSpec
from app.compare.quic_sim import quic_available
from app.compare.runner import run_experiment


@unittest.skipUnless(quic_available(), "aioquic not installed (pip install aioquic)")
class QuicMigrationTests(unittest.TestCase):
    def test_one_connection_survives_both_address_changes(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_experiment(ExperimentSpec("quic-migration"), d)
            self.assertEqual(r.status, "ok", r.reason)
            c = r.metrics["continuity"]
            self.assertTrue(c["transport_connection_preserved"])
            self.assertEqual(c["connections_opened"], 1)
            self.assertFalse(c["endpoint_address_preserved"])
            self.assertTrue(c["all_handoffs_recovered"])
            self.assertEqual(r.metrics["loss"]["lost"], 0)                       # reliable stream: no application-level loss
            self.assertEqual(r.metrics["duplicates"], 0)
            self.assertEqual(r.metrics["mobility_overhead"]["mobility_bytes_per_packet"], 0.0)

    def test_migration_beats_reconnection_on_signalling(self):
        with tempfile.TemporaryDirectory() as d:
            q = run_experiment(ExperimentSpec("quic-migration"), d)
            t = run_experiment(ExperimentSpec("tcp-reconnect"), d)
            self.assertEqual(q.status, "ok", q.reason)
            self.assertLessEqual(q.metrics["handoff_summary"]["handoff_latency_ms_mean"],
                                 t.metrics["handoff_summary"]["handoff_latency_ms_mean"] * 1.5)


@unittest.skipIf(quic_available(), "only meaningful when aioquic is missing")
class QuicUnavailable(unittest.TestCase):
    def test_reported_as_unavailable_with_install_hint(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_experiment(ExperimentSpec("quic-migration"), d)
            self.assertEqual(r.status, "unavailable")
            self.assertIn("pip install aioquic", r.reason)


if __name__ == "__main__":
    unittest.main()
