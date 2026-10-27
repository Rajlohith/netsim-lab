"""Live tests: build the real topology with `ip`/`tc` and measure it.

Skipped unless NETSIM_RUN_PRIVILEGED=1, `ip`, `tc`, `ping` exist and root / passwordless-cached
sudo is available. Uses its own prefix ("nslt-") so it never touches a lab created with "nsl-".
Run:  sudo -v && NETSIM_RUN_PRIVILEGED=1 python -m unittest discover -s backend/tests -t backend -p 'test_netem_integration.py'
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.netem.runner import Runner
from app.netem.topology import TopologyManager
from app.netem.topology_config import NetemSpec, load_config

CFG = Path(__file__).resolve().parents[2] / "config" / "topology.yaml"
PREFIX = "nslt-"
PROJECT = {f"{PREFIX}{k}" for k in ("cn", "ha", "mn", "ana", "anb")}


def _skip_reason():
    if os.environ.get("NETSIM_RUN_PRIVILEGED") != "1":
        return "set NETSIM_RUN_PRIVILEGED=1 to run live network tests"
    for tool in ("ip", "tc", "ping"):
        if shutil.which(tool) is None:
            return f"{tool} not installed"
    if os.geteuid() != 0:
        if shutil.which("sudo") is None or subprocess.run(["sudo", "-n", "true"]).returncode != 0:
            return "needs root or cached sudo credentials (run `sudo -v`)"
    return None


def _sh(argv):
    """Raw command (tests only) for creating/inspecting decoys the Runner would refuse."""
    pre = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return subprocess.run(pre + argv, capture_output=True, text=True)


def _host_default_routes():
    return subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout


@unittest.skipIf(_skip_reason() is not None, _skip_reason() or "")
class LiveTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(CFG)
        self.runner = Runner(PREFIX, dry_run=False)
        self.mgr = TopologyManager(self.cfg, self.runner, PREFIX, Path(self.tmp.name) / "state.json")
        self.mgr.destroy()                      # clear leftovers of an aborted earlier run
        self.mgr.create()
        self.addCleanup(self.mgr.destroy)

    # --- communication according to configured routes ---
    def test_every_node_reaches_every_address(self):
        for src in self.cfg.nodes:
            for dst in self.cfg.nodes:
                if src == dst:
                    continue
                for ip in self.cfg.addresses_of(dst):
                    with self.subTest(src=src, dst=dst, ip=str(ip)):
                        s = self.mgr.ping(src, str(ip), count=1, wait=2)
                        self.assertEqual(s.received, 1)

    def test_mn_prefers_access_network_a(self):
        r = self.runner.run(__import__("app.netem.plan", fromlist=["Cmd"]).Cmd(
            ("ip", "-n", f"{PREFIX}mn", "route", "get", "10.10.1.2"), "route get", readonly=True))
        self.assertIn("via 10.10.4.1", r.stdout)
        self.assertIn("eth-a", r.stdout)

    def test_forwarding_flags(self):
        from app.netem.plan import Cmd
        for key, expected in (("ha", "1"), ("ana", "1"), ("anb", "1"), ("cn", "0"), ("mn", "0")):
            r = self.runner.run(Cmd(("ip", "netns", "exec", f"{PREFIX}{key}", "sysctl", "-n",
                                     "net.ipv4.ip_forward"), "fwd", readonly=True))
            self.assertEqual(r.stdout.strip(), expected, key)

    def test_configured_netem_present(self):
        from app.netem.plan import Cmd
        r = self.runner.run(Cmd(("ip", "netns", "exec", f"{PREFIX}mn", "tc", "qdisc", "show", "dev", "eth-b"),
                                "qdisc", readonly=True))
        self.assertIn("netem", r.stdout)
        self.assertIn("delay 15", r.stdout.lower().replace("ms", ""))
        self.assertIn("rate 10mbit", r.stdout.lower())

    # --- measurable impairments ---
    def test_injected_latency_is_measurable(self):
        base = self.mgr.ping("mn", "10.10.1.2", count=10, interval=0.05)
        self.mgr.shape("ana-mn", "both", NetemSpec(delay_ms=40))   # was 5 ms each way -> +70 ms RTT
        shaped = self.mgr.ping("mn", "10.10.1.2", count=10, interval=0.05)
        self.assertGreater(shaped.rtt_avg_ms - base.rtt_avg_ms, 50)

    def test_injected_loss_is_measurable(self):
        self.mgr.shape("ha-ana", "both", NetemSpec(loss_percent=30))   # ~51% round-trip loss
        s = self.mgr.ping("mn", "10.10.1.2", count=100, interval=0.02, wait=1)
        self.assertTrue(25 <= s.loss_percent <= 80, s)

    # --- cleanup safety ---
    def test_destroy_removes_only_project_resources(self):
        decoys = ["decoy-keep", f"{PREFIX}extra"]     # one unrelated, one prefixed-but-not-in-config
        for d in decoys:
            self.assertEqual(_sh(["ip", "netns", "add", d]).returncode, 0)
        self.addCleanup(lambda: [_sh(["ip", "netns", "del", d]) for d in decoys])
        routes_before = _host_default_routes()
        report = self.mgr.destroy()
        self.assertEqual(set(report.deleted), PROJECT)
        remaining = set(self.mgr.list_namespaces())
        self.assertTrue(set(decoys) <= remaining)
        self.assertFalse(PROJECT & remaining)
        self.assertEqual(_host_default_routes(), routes_before)   # host default route untouched


if __name__ == "__main__":
    unittest.main()
