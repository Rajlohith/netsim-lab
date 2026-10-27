"""LIVE acceptance test: real kernel IP-in-IP tunnels + real daemons inside the namespace testbed.

Skipped unless NETSIM_RUN_PRIVILEGED=1 with ip/ping available, sudo (or root), and a kernel that can create
ipip tunnels. Uses prefix "nslt-" so it never touches a lab built with "nsl-".
    sudo -v && NETSIM_RUN_PRIVILEGED=1 python -m unittest tests.test_mip_live     (from backend/)
NOT executed in the development sandbox (no iproute2 / sudo there): treat as unverified until you run it.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.mobileip.demo import DemoSpec, run_demo
from app.netem.runner import Runner
from app.netem.topology import TopologyManager
from app.netem.topology_config import load_config
from app.traffic.loader import read_csv

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "nslt-"


def _skip_reason():
    if os.environ.get("NETSIM_RUN_PRIVILEGED") != "1":
        return "set NETSIM_RUN_PRIVILEGED=1 to run live Mobile IP tests"
    for tool in ("ip", "tc", "ping", "setpriv", "unshare"):
        if shutil.which(tool) is None:
            return f"{tool} not installed"
    if os.geteuid() == 0:
        return "run as a normal user (the demo refuses root)"
    if subprocess.run(["sudo", "-n", "true"]).returncode != 0:
        return "needs cached sudo credentials (run `sudo -v`)"
    probe = subprocess.run(["unshare", "-Urn", "ip", "tunnel", "add", "tnl-probe", "mode", "ipip", "local",
                            "127.0.0.1", "remote", "127.0.0.2"], capture_output=True)
    if probe.returncode != 0:
        return "kernel cannot create ipip tunnels (try: sudo modprobe ipip)"
    return None


def _sh(argv):
    return subprocess.run(["sudo", "-n"] + argv, capture_output=True, text=True)


@unittest.skipIf(_skip_reason() is not None, _skip_reason() or "")
class LiveMobileIpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = load_config(ROOT / "config" / "topology.yaml")
        self.mgr = TopologyManager(cfg, Runner(PREFIX, dry_run=False), PREFIX, Path(self.tmp.name) / "state.json")
        self.mgr.destroy()
        self.mgr.create()
        self.addCleanup(self.mgr.destroy)

    def test_handoff_with_real_tunnels(self):
        spec = DemoSpec(scenario_id="live-mip", duration_s=9.0, rate_pps=50, moves=((3.0, "eth-b"), (6.0, "eth-a")))
        run_dir, m = run_demo(spec, self.mgr, Path(self.tmp.name))
        # packets reached the mobile node (addressed to its HOME address) through the Home Agent tunnel
        rx = read_csv(run_dir / "received.csv", "received.csv").rows
        self.assertGreater(len(rx), 200)
        self.assertEqual({r["src_addr"] for r in rx}, {"10.10.1.2"})
        # the binding followed the mobile node
        via = [t["reachable_via"] for t in m["binding_timeline"]]
        self.assertEqual(via, ["10.10.4.2", "10.10.5.2", "10.10.4.2", None])
        self.assertEqual([e["outcome"] for e in m["episodes"]], ["registered"] * 3)
        self.assertIsNotNone(m["connectivity"]["handoffs"][0]["interruption_ms"])
        # cleanup: no tunnels, no home addresses left in the lab; MN interfaces restored
        for ns, name in ((f"{PREFIX}ha", "tnl-mn"), (f"{PREFIX}mn", "tnl-ha")):
            self.assertNotIn(name, _sh(["ip", "-n", ns, "link", "show"]).stdout)
        self.assertNotIn("10.10.100.2", _sh(["ip", "-n", f"{PREFIX}mn", "addr", "show", "lo"]).stdout)
        self.assertNotIn("10.10.100.1", _sh(["ip", "-n", f"{PREFIX}ha", "addr", "show", "lo"]).stdout)
        links = _sh(["ip", "-n", f"{PREFIX}mn", "-br", "link", "show"]).stdout
        self.assertEqual(links.count("UP"), 3)                                    # lo + eth-a + eth-b


if __name__ == "__main__":
    unittest.main()
