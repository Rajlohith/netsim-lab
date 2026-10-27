"""LIVE tests on the real Linux kernel: the project's own Stage 2 command plans are executed (via netlink) and the
Stage 3 probe tools run in the resulting namespaces. REAL kernel veth, routing, forwarding and UDP; no emulation.

Not covered (this kernel has no ipip/gre/dummy and no visible netem; iproute2 is not installed): IP-in-IP tunnels,
tc netem delay/loss/rate, and the `ip`/`tc` command-line syntax itself. Run elsewhere with:  unshare -Urn python -m unittest tests.test_live_kernel_lab
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from app.netem.errors import NetemError
from app.netem.runner import CmdResult, Runner
from app.netem.topology import TopologyManager
from app.netem.topology_config import parse_config
from app.traffic.analyze import analyze_run
from app.traffic.loader import read_csv
from tests import kernel_lab as K

ROOT = Path(__file__).resolve().parents[2]
SKIP = K.skip_reason()


def config(name="topology.yaml"):
    data = yaml.safe_load((ROOT / "config" / name).read_text())
    for l in data["links"]:
        l.pop("netem", None)                  # tc netem cannot run in the kernel lab: the topology is built WITHOUT impairments
    return parse_config(data)


@unittest.skipIf(SKIP is not None, SKIP or "")
class KernelLab(unittest.TestCase):
    PREFIX = "nsl-"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ex = K.KernelIpExecutor()
        self.cfg = config()
        self.mgr = TopologyManager(self.cfg, Runner(self.PREFIX, dry_run=False, use_sudo=False, executor=self.ex),
                                   self.PREFIX, Path(self.tmp.name) / "state.json")
        self.addCleanup(self.ex.cleanup)
        self.addCleanup(self.mgr.destroy)

    def tool(self, ns, module, *args):
        env = dict(os.environ, PYTHONPATH=K.BACKEND)
        return subprocess.Popen(K.ns_cmd(self.ex, ns, sys.executable, "-m", module, *map(str, args)), cwd=K.BACKEND, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class Topology(KernelLab):
    def test_the_stage2_plan_builds_the_documented_lab_on_the_real_kernel(self):
        created = self.mgr.create()
        self.assertEqual(sorted(created), ["nsl-ana", "nsl-anb", "nsl-cn", "nsl-ha", "nsl-mn"])
        want = {"nsl-cn": {"lo", "eth-ha"}, "nsl-ha": {"lo", "eth-cn", "eth-ana", "eth-anb"},
                "nsl-ana": {"lo", "eth-ha", "eth-mn"}, "nsl-anb": {"lo", "eth-ha", "eth-mn"}, "nsl-mn": {"lo", "eth-a", "eth-b"}}
        for ns, ifs in want.items():
            self.assertEqual(set(self.ex.interfaces(ns)), ifs, ns)
        for key, node in self.cfg.nodes.items():                               # addresses exactly as configured
            addrs = self.ex.addresses(f"nsl-{key}")
            for l, e in self.cfg.interfaces_of(key):
                self.assertEqual(addrs[e.ifname], str(e.address.ip), (key, e.ifname))
        for key, node in self.cfg.nodes.items():                               # forwarding only on the routers, rp_filter off
            self.assertEqual(self.ex.sysctl(f"nsl-{key}", "net.ipv4.ip_forward"), "1" if node.forwarding else "0", key)
            self.assertEqual(self.ex.sysctl(f"nsl-{key}", "net.ipv4.conf.all.rp_filter"), "0", key)

    def test_routes_and_metrics_are_what_the_yaml_says(self):
        self.mgr.create()
        mn = [r for r in K.read_routes(self.ex, "nsl-mn") if r[1] == "0.0.0.0"]
        self.assertEqual(sorted((r[0], r[2], r[3]) for r in mn), [("eth-a", "10.10.4.1", 100), ("eth-b", "10.10.5.1", 200)])
        ha = {(r[1], r[2]) for r in K.read_routes(self.ex, "nsl-ha")}
        self.assertIn(("10.10.4.0", "10.10.2.2"), ha)
        self.assertIn(("10.10.5.0", "10.10.3.2"), ha)

    def test_packets_really_cross_the_routed_veth_chain(self):
        self.mgr.create()
        for src, dst, ip in (("nsl-cn", "nsl-mn", "10.10.4.2"), ("nsl-mn", "nsl-cn", "10.10.1.2"), ("nsl-cn", "nsl-mn", "10.10.5.2"),
                             ("nsl-ana", "nsl-anb", "10.10.3.2"), ("nsl-mn", "nsl-ha", "10.10.2.1")):
            self.assertEqual(K.udp_roundtrip(self.ex, src, dst, ip), 5, f"{src}->{dst} {ip}")

    def test_transit_is_blocked_when_forwarding_is_off(self):
        """Expected failure on the real kernel: the same path stops working when a router stops forwarding."""
        self.mgr.create()
        self.assertEqual(K.udp_roundtrip(self.ex, "nsl-cn", "nsl-mn", "10.10.4.2"), 5)
        self.ex(["ip", "netns", "exec", "nsl-ana", "sysctl", "-w", "net.ipv4.ip_forward=0"], 5)
        self.assertEqual(K.udp_roundtrip(self.ex, "nsl-cn", "nsl-mn", "10.10.4.2"), 0)

    def test_a_missing_route_makes_the_destination_unreachable(self):
        data = yaml.safe_load((ROOT / "config" / "topology.yaml").read_text())
        for l in data["links"]:
            l.pop("netem", None)
        data["routes"] = [r for r in data["routes"] if not (r["node"] == "mn")]       # mobile node loses its default routes
        mgr = TopologyManager(parse_config(data), Runner(self.PREFIX, dry_run=False, use_sudo=False, executor=self.ex),
                              self.PREFIX, None)
        mgr.create()
        try:
            self.assertEqual(K.udp_roundtrip(self.ex, "nsl-mn", "nsl-cn", "10.10.1.2"), 0)
            self.assertEqual(K.udp_roundtrip(self.ex, "nsl-mn", "nsl-ana", "10.10.4.1"), 5)   # directly connected still works
        finally:
            mgr.destroy()


class RealProbes(KernelLab):
    def run_probes(self, duration=2.5, rate=200, toggle_at=None, toggle=None):
        self.mgr.create()
        out = Path(self.tmp.name) / "res"
        common = ["--scenario-id", "kernel", "--run-id", "r1", "--out-dir", out]
        rx = self.tool("nsl-cn", "app.traffic.receiver", *common, "--bind", "10.10.1.2", "--port", 5005, "--idle-timeout", 1.0,
                       "--startup-timeout", 10)
        time.sleep(0.5)
        tx = self.tool("nsl-mn", "app.traffic.sender", "10.10.1.2:5005", *common, "--rate", rate, "--duration", duration, "--drain", 0.5)
        if toggle_at is not None:
            time.sleep(toggle_at)
            toggle()
        self.assertEqual(tx.wait(timeout=30), 0, tx.stderr.read())
        rx.wait(timeout=30)
        for p in (tx, rx):
            p.stdout.close(), p.stderr.close()
        return out / "kernel" / "r1"

    def test_stage3_probes_run_unmodified_across_real_namespaces(self):
        run = self.run_probes(duration=1.5, rate=200)
        m = analyze_run(run)
        self.assertEqual((m["loss"]["lost"], m["loss"]["sent_ok"], m["loss"]["exact"]), (0, 300, True))
        rx = read_csv(run / "received.csv", "received.csv").rows
        self.assertEqual({r["src_addr"] for r in rx}, {"10.10.4.2"})              # MN's address on network A
        self.assertEqual(m["owd_ms"]["count"], 300)                               # same kernel boot -> one-way delay is valid
        self.assertLess(m["rtt_ms"]["p50"], 20.0)                                 # REAL measured RTT over three veth hops (no netem)
        self.assertGreater(m["rtt_ms"]["p50"], 0.0)

    def test_real_failover_to_the_standby_access_network(self):
        run = self.run_probes(duration=3.0, rate=200, toggle_at=1.0,
                              toggle=lambda: self.mgr.set_interface("mn", "eth-a", False))
        m = analyze_run(run)
        rx = read_csv(run / "received.csv", "received.csv").rows
        srcs = [r["src_addr"] for r in rx]
        self.assertEqual(srcs[0], "10.10.4.2")
        self.assertEqual(srcs[-1], "10.10.5.2")                                   # the kernel moved the flow to network B by itself
        switch = next(i for i, s in enumerate(srcs) if s == "10.10.5.2")
        self.assertTrue(all(s == "10.10.5.2" for s in srcs[switch:]))             # and never flapped back
        self.assertLessEqual(m["loss"]["lost"], 20)                               # real, measured: a handful of packets at most
        gaps = m["interarrival_gaps_ms"]
        self.assertLess(gaps["max"], 200.0)                                       # service resumed within 200 ms (measured, not modelled)
        routes = [r for r in K.read_routes(self.ex, "nsl-mn") if r[1] == "0.0.0.0"]
        self.assertEqual([(r[0], r[3]) for r in routes], [("eth-b", 200)])       # eth-a's default route was removed with the link


class StaleCareOfAddress(KernelLab):
    """What really happens to packets sent to a care-of address that has vanished (Stage 4 documentation claim)."""

    def test_only_the_mobile_node_end_down_gives_host_unreachable_not_a_loop(self):
        self.mgr.create()
        self.mgr.set_interface("mn", "eth-a", False)                              # what the live demo does
        self.assertEqual(self.ex.link_state("nsl-ana", "eth-mn"), {"admin_up": True, "carrier": False})
        seen = K.icmp_responses(self.ex, "nsl-cn", "10.10.4.2", 4.5)
        types = {(s[1], s[2]) for s in seen}
        self.assertIn((3, 1), types)                                              # destination host unreachable (ARP failed)
        self.assertNotIn(11, {s[1] for s in seen})                                # no TTL-exceeded: the packets do NOT loop
        self.assertTrue(all(s[0] == "10.10.2.2" for s in seen), seen)             # reported by the access router

    def test_both_ends_down_makes_stale_packets_loop_until_ttl_expiry(self):
        self.mgr.create()
        self.mgr.set_interface("mn", "eth-a", False)
        self.mgr.set_interface("ana", "eth-mn", False)                            # the router's connected route disappears too
        seen = K.icmp_responses(self.ex, "nsl-cn", "10.10.4.2", 1.5)
        self.assertIn(11, {s[1] for s in seen})                                   # TTL exceeded: HA <-> access router loop


class CleanupAndSafety(KernelLab):
    def test_destroy_removes_only_project_namespaces_and_leaves_the_host_alone(self):
        with open("/proc/net/dev") as f:
            host_before = f.read().splitlines()[2:]
        self.ex(["ip", "netns", "add", "decoy"], 5)
        self.ex(["ip", "netns", "add", "nsl-extra"], 5)                            # prefixed, but not part of the config
        self.mgr.create()
        report = self.mgr.destroy()
        self.assertEqual(sorted(report.deleted), ["nsl-ana", "nsl-anb", "nsl-cn", "nsl-ha", "nsl-mn"])
        self.assertEqual(set(self.ex.ns), {"decoy", "nsl-extra"})
        with open("/proc/net/dev") as f:
            host_after = f.read().splitlines()[2:]
        self.assertEqual([l.split(":")[0] for l in host_after], [l.split(":")[0] for l in host_before])   # host namespace unchanged
        self.assertEqual(self.ex.interfaces("decoy"), ["lo"])                      # no veth end leaked into the decoy

    def test_create_refuses_to_reuse_existing_namespaces(self):
        self.ex(["ip", "netns", "add", "nsl-ha"], 5)
        from app.netem.errors import TopologyError
        with self.assertRaises(TopologyError):
            self.mgr.create()
        self.assertEqual(set(self.ex.ns), {"nsl-ha"})                              # nothing else was created

    def test_failed_create_rolls_back_on_the_real_kernel(self):
        real = self.ex

        class Failing:
            def __init__(self):
                self.n = 0

            def __call__(self, argv, timeout):
                self.n += 1
                if self.n == 40:
                    return CmdResult(tuple(argv), 1, "", "injected failure")
                return real(argv, timeout)
        mgr = TopologyManager(self.cfg, Runner(self.PREFIX, dry_run=False, use_sudo=False, executor=Failing()), self.PREFIX, None)
        with self.assertRaises(NetemError):
            mgr.create()
        self.assertEqual(set(real.ns), set())                                      # every namespace created so far was removed


if __name__ == "__main__":
    unittest.main()
