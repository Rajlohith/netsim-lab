import io
import os
import unittest
from unittest import mock

from app.netem.cli import main
from app.netem.errors import NetemError
from app.netem.measure import parse_ping

LOSSY = """--- 10.10.1.2 ping statistics ---
100 packets transmitted, 47 received, 53% packet loss, time 2400ms
rtt min/avg/max/mdev = 20.1/25.5/40.2/3.3 ms
"""
TOTAL_LOSS = """--- 10.10.1.2 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2050ms
"""
BUSYBOX = """--- 10.10.1.2 ping statistics ---
4 packets transmitted, 4 packets received, 0% packet loss
round-trip min/avg/max = 0.100/0.200/0.300 ms
"""


class ParseTests(unittest.TestCase):
    def test_lossy(self):
        s = parse_ping(LOSSY)
        self.assertEqual((s.transmitted, s.received, s.loss_percent, s.rtt_avg_ms), (100, 47, 53.0, 25.5))

    def test_total_loss_has_no_rtt(self):
        s = parse_ping(TOTAL_LOSS)
        self.assertEqual(s.loss_percent, 100.0)
        self.assertIsNone(s.rtt_avg_ms)

    def test_busybox(self):
        self.assertEqual(parse_ping(BUSYBOX).rtt_max_ms, 0.3)

    def test_garbage(self):
        with self.assertRaises(NetemError):
            parse_ping("nonsense")


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = main(list(argv), out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_validate(self):
        rc, out, _ = self.run_cli("validate")
        self.assertEqual(rc, 0)
        self.assertIn("5 nodes", out)

    def test_plan_prints_commands(self):
        rc, out, _ = self.run_cli("plan")
        self.assertEqual(rc, 0)
        self.assertIn("sudo ip netns add nsl-cn", out)
        self.assertNotIn("iptables", out)

    def test_bad_config_returns_error(self):
        rc, _, err = self.run_cli("validate", "--config", "/nonexistent.yaml")
        self.assertEqual(rc, 1)
        self.assertIn("cannot read", err)

    def test_refuses_root_without_flag(self):
        with mock.patch("app.netem.cli.os.geteuid", return_value=0):
            rc, _, err = self.run_cli("create")
        self.assertEqual(rc, 2)
        self.assertIn("Refusing to run as root", err)

    def test_create_defaults_to_dry_run(self):
        with mock.patch("app.netem.cli.os.geteuid", return_value=1000), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NETSIM_DRY_RUN", None)
            rc, out, _ = self.run_cli("create")
        self.assertEqual(rc, 0)
        self.assertIn("[dry-run] nothing executed", out)


if __name__ == "__main__":
    unittest.main()
