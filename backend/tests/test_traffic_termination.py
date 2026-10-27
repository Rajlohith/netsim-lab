"""Process termination and incomplete runs, using real sender/receiver processes."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.traffic import sender as sender_mod
from app.traffic.analyze import analyze_run
from app.traffic.loader import read_csv
from tests.traffic_helpers import BACKEND

ENV = dict(os.environ, PYTHONPATH=str(BACKEND))


def spawn(module, *args):
    return subprocess.Popen([sys.executable, "-m", f"app.traffic.{module}", *map(str, args)],
                            cwd=BACKEND, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_file(path, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if Path(path).exists() and Path(path).read_text().strip():
            return Path(path).read_text().strip()
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


class TerminationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()

    def start_pair(self, sender_args=(), receiver_args=()):
        ready = self.out / "ready"
        rx = spawn("receiver", "--scenario-id", "term", "--run-id", "r1", "--out-dir", self.out, "--port", 0,
                   "--bind", "127.0.0.1", "--ready-file", ready, "--idle-timeout", 1.0, *receiver_args)
        self.procs.append(rx)
        port = wait_file(ready)
        tx = spawn("sender", f"127.0.0.1:{port}", "--scenario-id", "term", "--run-id", "r1", "--out-dir", self.out,
                   "--rate", 200, "--duration", 60, "--drain", 0.3, *sender_args)
        self.procs.append(tx)
        run_dir = self.out / "term" / "r1"
        end = time.time() + 10
        while time.time() < end and not (run_dir / "sent.csv").exists():
            time.sleep(0.02)
        time.sleep(0.7)                                  # let some traffic flow
        return rx, tx, run_dir

    def meta(self, run_dir, role):
        return json.loads((run_dir / f"{role}.meta.json").read_text())

    def test_sigterm_sender_finalises_cleanly(self):
        rx, tx, run_dir = self.start_pair()
        tx.send_signal(signal.SIGTERM)
        self.assertEqual(tx.wait(timeout=10), 0)
        rx.wait(timeout=10)
        sm = self.meta(run_dir, "sender")
        self.assertEqual((sm["status"], sm["stop_reason"]), ("interrupted", "signal:SIGTERM"))
        self.assertIsNotNone(sm["end_mono_ns"])
        sent = read_csv(run_dir / "sent.csv", "sent.csv")
        self.assertEqual(len(sent.rows), sm["counters"]["sent_rows"])
        self.assertFalse(sent.truncated_tail)
        self.assertGreater(len(sent.rows), 50)
        m = analyze_run(run_dir)
        self.assertEqual(m["run_status"], "interrupted")
        self.assertTrue(m["loss"]["exact"])
        self.assertEqual(m["loss"]["lost"], 0)
        self.assertEqual(m["loss"]["sent_ok"], len(sent.rows))

    def test_sigterm_receiver_finalises_cleanly(self):
        rx, tx, run_dir = self.start_pair()
        rx.send_signal(signal.SIGTERM)
        self.assertEqual(rx.wait(timeout=10), 0)
        rm = self.meta(run_dir, "receiver")
        self.assertEqual((rm["status"], rm["stop_reason"]), ("interrupted", "signal:SIGTERM"))
        rows = read_csv(run_dir / "received.csv", "received.csv")
        self.assertEqual(len(rows.rows), rm["counters"]["received_rows"])
        tx.send_signal(signal.SIGTERM)
        tx.wait(timeout=10)
        m = analyze_run(run_dir)
        # packets sent after the receiver stopped look lost, so loss must not be claimed as exact
        self.assertIn(m["run_status"], ("interrupted",))
        self.assertGreater(m["loss"]["lost"], 0)

    def test_sigkill_sender_leaves_detectably_incomplete_run(self):
        rx, tx, run_dir = self.start_pair()
        tx.kill()
        tx.wait(timeout=10)
        rx.wait(timeout=10)
        self.assertEqual(self.meta(run_dir, "sender")["status"], "running")   # never finalised
        m = analyze_run(run_dir)
        self.assertEqual(m["run_status"], "incomplete")
        self.assertFalse(m["loss"]["exact"])
        self.assertTrue(any("sender status is 'running'" in w for w in m["warnings"]))
        sent_rows = m["inputs"]["sent.csv"]["rows"]
        self.assertEqual(m["loss"]["sent_ok"], sent_rows)        # only logged probes are counted, none invented
        self.assertGreater(sent_rows, 50)

    def test_stop_file_requests_clean_shutdown(self):
        stop_tx, stop_rx = self.out / "tx.stop", self.out / "rx.stop"
        rx, tx, run_dir = self.start_pair(sender_args=("--stop-file", stop_tx), receiver_args=("--stop-file", stop_rx))
        stop_tx.write_text("x")
        self.assertEqual(tx.wait(timeout=10), 0)
        stop_rx.write_text("x")
        self.assertEqual(rx.wait(timeout=10), 0)
        self.assertEqual(self.meta(run_dir, "sender")["stop_reason"], "stop_file")
        self.assertEqual(self.meta(run_dir, "receiver")["stop_reason"], "stop_file")
        self.assertEqual(analyze_run(run_dir)["run_status"], "interrupted")

    def test_raw_data_is_never_overwritten(self):
        _, tx, run_dir = self.start_pair()
        tx.send_signal(signal.SIGTERM)
        tx.wait(timeout=10)
        before = (run_dir / "sent.csv").read_bytes()
        rc = sender_mod.main(["127.0.0.1:9", "--scenario-id", "term", "--run-id", "r1", "--out-dir", str(self.out),
                              "--count", "1"])
        self.assertEqual(rc, 2)
        self.assertEqual((run_dir / "sent.csv").read_bytes(), before)

    def test_receiver_with_no_traffic_ends_by_startup_timeout(self):
        rx = spawn("receiver", "--scenario-id", "quiet", "--run-id", "r1", "--out-dir", self.out, "--port", 0,
                   "--bind", "127.0.0.1", "--startup-timeout", 0.3)
        self.procs.append(rx)
        self.assertEqual(rx.wait(timeout=10), 0)
        run_dir = self.out / "quiet" / "r1"
        self.assertEqual(self.meta(run_dir, "receiver")["stop_reason"], "startup_timeout")
        m = analyze_run(run_dir)
        self.assertIsNone(m["loss"]["loss_percent"])               # no data -> no numbers
        self.assertEqual(m["interarrival_gaps_ms"]["count"], 0)

    def test_invalid_datagrams_are_counted_not_recorded(self):
        import socket
        ready = self.out / "ready2"
        rx = spawn("receiver", "--scenario-id", "junk", "--run-id", "r1", "--out-dir", self.out, "--port", 0,
                   "--bind", "127.0.0.1", "--ready-file", ready, "--startup-timeout", 1.0)
        self.procs.append(rx)
        port = int(wait_file(ready))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for junk in (b"hello", b"", b"NSP1" + b"\x00" * 3):
                s.sendto(junk, ("127.0.0.1", port))
        rx.wait(timeout=10)
        rm = self.meta(self.out / "junk" / "r1", "receiver")
        self.assertEqual(rm["counters"]["invalid_datagrams"], 3)     # incl. the zero-length datagram
        self.assertEqual(rm["counters"]["received_valid"], 0)


if __name__ == "__main__":
    unittest.main()
