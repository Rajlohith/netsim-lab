import json
import tempfile
import unittest
from pathlib import Path

from app.netem.errors import SafetyError, TopologyError
from app.netem.runner import Runner
from app.netem.topology import TopologyManager
from app.netem.topology_config import NetemSpec, load_config
from tests.netem_fakes import FakeHost

CFG = Path(__file__).resolve().parents[2] / "config" / "topology.yaml"
P = "nsl-"
PROJECT = {"nsl-cn", "nsl-ha", "nsl-mn", "nsl-ana", "nsl-anb"}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"

    def mk(self, host, dry_run=False):
        runner = Runner(P, dry_run=dry_run, use_sudo=False, executor=host)
        return TopologyManager(load_config(CFG), runner, P, self.state), runner

    def test_create_makes_exactly_the_project_namespaces(self):
        host = FakeHost(namespaces={"decoy", "docker0"})
        mgr, _ = self.mk(host)
        mgr.create()
        self.assertEqual(host.namespaces, PROJECT | {"decoy", "docker0"})
        self.assertEqual(set(json.loads(self.state.read_text())["namespaces"]), PROJECT)

    def test_create_refuses_if_namespace_exists(self):
        host = FakeHost(namespaces={"nsl-ha"})
        mgr, _ = self.mk(host)
        with self.assertRaises(TopologyError):
            mgr.create()
        self.assertEqual(host.mutating(), [])        # nothing was changed

    def test_failed_create_rolls_back_only_its_own_namespaces(self):
        host = FakeHost(namespaces={"decoy"}, fail_on_call=40)
        mgr, _ = self.mk(host)
        with self.assertRaises(Exception):
            mgr.create()
        self.assertEqual(host.namespaces, {"decoy"})
        self.assertFalse(self.state.exists())

    def test_destroy_removes_only_project_namespaces(self):
        host = FakeHost(namespaces={"decoy", "nsl-extra", "default"})  # nsl-extra: prefixed, not in config
        mgr, _ = self.mk(host)
        mgr.create()
        report = mgr.destroy()
        self.assertEqual(set(report.deleted), PROJECT)
        self.assertEqual(host.namespaces, {"decoy", "nsl-extra", "default"})
        self.assertFalse(self.state.exists())

    def test_destroy_refuses_tampered_state_entries(self):
        host = FakeHost(namespaces={"default", "myns", "nsl-cn"})
        self.state.write_text(json.dumps({"namespaces": ["default", "myns", "../x", "nsl-CN", "nsl-cn"]}))
        mgr, _ = self.mk(host)
        report = mgr.destroy()
        self.assertEqual(report.deleted, ["nsl-cn"])
        self.assertEqual(set(report.refused), {"default", "myns", "../x", "nsl-CN"})
        self.assertEqual(host.namespaces, {"default", "myns"})
        self.assertTrue(self.state.exists())          # kept: something was refused

    def test_corrupt_state_file_is_ignored(self):
        self.state.write_text("{not json")
        host = FakeHost(namespaces={"decoy"})
        mgr, _ = self.mk(host)
        self.assertEqual(mgr.destroy().deleted, [])
        self.assertEqual(host.namespaces, {"decoy"})

    def test_destroy_is_idempotent(self):
        host = FakeHost()
        mgr, _ = self.mk(host)
        mgr.create()
        mgr.destroy()
        self.assertEqual(mgr.destroy().deleted, [])

    def test_dry_run_executes_no_mutating_commands(self):
        host = FakeHost()
        mgr, runner = self.mk(host, dry_run=True)
        mgr.create()
        mgr.destroy()
        self.assertEqual(host.mutating(), [])
        self.assertTrue(len(runner.history) > 60)
        self.assertFalse(self.state.exists())

    def test_sudo_prefix_applied_only_when_unprivileged(self):
        host = FakeHost()
        runner = Runner(P, dry_run=False, use_sudo=True, executor=host)
        import shutil
        if shutil.which("sudo") is None:
            self.skipTest("sudo not installed")
        # FakeHost strips the sudo prefix itself; just ensure the call succeeds end to end
        mgr = TopologyManager(load_config(CFG), runner, P, self.state)
        mgr.create()
        self.assertEqual(host.namespaces, PROJECT)

    def test_shape_and_clear(self):
        host = FakeHost()
        mgr, _ = self.mk(host)
        mgr.create()
        done = mgr.shape("ana-mn", "both", NetemSpec(delay_ms=40))
        self.assertEqual(done, ["nsl-ana:eth-mn", "nsl-mn:eth-a"])
        self.assertIn(("ip", "netns", "exec", "nsl-mn", "tc", "qdisc", "replace", "dev", "eth-a", "root",
                       "netem", "delay", "40ms"), host.calls)
        mgr.shape("ana-mn", "a", None)
        self.assertIn(("ip", "netns", "exec", "nsl-ana", "tc", "qdisc", "del", "dev", "eth-mn", "root"), host.calls)

    def test_shape_requires_existing_namespaces_and_known_link(self):
        mgr, _ = self.mk(FakeHost())
        with self.assertRaises(TopologyError):
            mgr.shape("ana-mn", "both", NetemSpec(delay_ms=1))
        with self.assertRaises(Exception):
            mgr.shape("nope", "both", NetemSpec(delay_ms=1))

    def test_ping_parses_output_and_rejects_bad_targets(self):
        host = FakeHost()
        mgr, _ = self.mk(host)
        mgr.create()
        s = mgr.ping("mn", "10.10.1.2", count=5)
        self.assertEqual((s.transmitted, s.received, s.loss_percent, s.rtt_avg_ms), (5, 5, 0.0, 10.5))
        with self.assertRaises(SafetyError):
            mgr.ping("mn", "10.10.1.2; reboot")
        with self.assertRaises(TopologyError):
            mgr.ping("ghost", "10.10.1.2")


if __name__ == "__main__":
    unittest.main()
