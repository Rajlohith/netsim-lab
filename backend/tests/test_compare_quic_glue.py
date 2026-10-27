"""QUIC adapter GLUE test against a STUB aioquic (tests/stubs/). Verifies only OUR code in app/compare/quic_sim.py:
event dispatch, timers, source-address switching on a network change, stream wiring, reporting.
It does NOT verify aioquic, QUIC, or real connection migration. Skipped when the real aioquic is installed
(then tests/test_compare_quic.py exercises the real library)."""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

from app.compare.experiment import ExperimentSpec
from app.compare.quic_sim import QuicLab, quic_available
from app.compare.runner import run_experiment

STUBS = str(Path(__file__).resolve().parent / "stubs")
REAL = quic_available()


class stub_aioquic:
    def __enter__(self):
        sys.path.insert(0, STUBS)
        importlib.invalidate_caches()
        return self

    def __exit__(self, *exc):
        sys.path.remove(STUBS)
        for m in [m for m in sys.modules if m == "aioquic" or m.startswith("aioquic.")]:
            del sys.modules[m]
        importlib.invalidate_caches()


@unittest.skipIf(REAL, "real aioquic installed: see test_compare_quic.py")
class QuicGlueWithStub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_adapter_drives_one_connection_across_both_address_changes(self):
        with stub_aioquic():
            lab = QuicLab(ExperimentSpec("quic-migration", scenario_id="glue"), self.tmp.name)
            run = lab.run()
            from app.compare.normalize import normalize
            m = normalize(run)
            self.assertEqual(m["loss"]["lost"], 0)
            self.assertEqual(m["duplicates"], 0)
            self.assertTrue(m["continuity"]["transport_connection_preserved"])
            self.assertEqual(m["continuity"]["connections_opened"], 1)
            self.assertTrue(m["continuity"]["all_handoffs_recovered"])
            server = lab.server_ep.conn
            self.assertEqual(server.peer_history, ["10.10.4.2", "10.10.5.2", "10.10.4.2"])   # initial address, then two real switches
            self.assertEqual(lab.client_ep.conn.cid_changes, 2)                           # fresh connection ID per migration
            self.assertEqual(lab.client_ep.conn.pings_sent, 2)                            # packet provoked from each new address
            self.assertIn("STUB", m["model"]["transport"])                               # the result says what engine produced it

    def test_through_the_common_interface_with_the_stub_present(self):
        with stub_aioquic():
            r = run_experiment(ExperimentSpec("quic-migration", scenario_id="glue2"), self.tmp.name)
            self.assertEqual(r.status, "ok", r.reason)
            self.assertEqual(r.metrics["mobility_overhead"]["mobility_bytes_per_packet"], 0.0)
            self.assertEqual(r.metrics["provenance"]["data_origin"], "simulated")

    def test_stub_does_not_leak_into_other_tests(self):
        with stub_aioquic():
            self.assertTrue(quic_available())
        self.assertFalse(quic_available())
        self.assertNotIn("aioquic", sys.modules)


if __name__ == "__main__":
    unittest.main()
