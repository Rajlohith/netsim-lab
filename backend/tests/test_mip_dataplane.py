"""Linux data-plane command plans, the file handshake and the allow-list (no root needed)."""
import tempfile
import unittest
from pathlib import Path

from app.mobileip import dataplane as dp
from app.mobileip.applier import LinuxDataPlaneApplier
from app.netem.errors import SafetyError
from app.netem.runner import Runner
from app.netem.safety import validate_argv
from tests.netem_fakes import FakeHost

P = "nsl-"
C = dp.LinuxMipConfig()


def all_plans():
    return (dp.linux_setup_home(C) + dp.linux_ha_install(C, "10.10.4.2", False) + dp.linux_ha_install(C, "10.10.5.2", True)
            + dp.linux_ha_remove(C) + dp.linux_mn_set_care_of(C, "10.10.4.2", False)
            + dp.linux_mn_set_care_of(C, "10.10.5.2", True) + dp.linux_mn_clear(C) + dp.linux_teardown_home(C))


class PlanTests(unittest.TestCase):
    def test_all_commands_are_allow_listed_and_namespaced(self):
        for c in all_plans():
            validate_argv(c.argv, P)
            self.assertEqual(c.argv[:2], ("ip", "-n"))
            self.assertIn(c.argv[2], ("nsl-ha", "nsl-mn"))

    def test_ha_tunnel_commands(self):
        add = [c.argv for c in dp.linux_ha_install(C, "10.10.4.2", False)]
        self.assertEqual(add[0], ("ip", "-n", "nsl-ha", "tunnel", "add", "tnl-mn", "mode", "ipip", "local",
                                  "10.10.100.1", "remote", "10.10.4.2"))
        self.assertEqual(add[1], ("ip", "-n", "nsl-ha", "link", "set", "tnl-mn", "up"))
        self.assertEqual(add[2], ("ip", "-n", "nsl-ha", "route", "replace", "10.10.100.2/32", "dev", "tnl-mn"))
        change = [c.argv for c in dp.linux_ha_install(C, "10.10.5.2", True)]
        self.assertEqual(change[0][3:5], ("tunnel", "change"))
        self.assertEqual(len(change), 2)                                   # no second link-up
        self.assertEqual([c.argv[3:5] for c in dp.linux_ha_remove(C)], [("route", "del"), ("tunnel", "del")])

    def test_mn_tunnel_decapsulates_from_the_agent_address(self):
        add = [c.argv for c in dp.linux_mn_set_care_of(C, "10.10.5.2", False)][0]
        self.assertEqual(add[3:], ("tunnel", "add", "tnl-ha", "mode", "ipip", "local", "10.10.5.2", "remote", "10.10.100.1"))

    def test_malicious_variants_rejected(self):
        t = lambda *a: ("ip", "-n", "nsl-ha") + a
        bad = [
            t("tunnel", "add", "tnl-x", "mode", "gre", "local", "10.0.0.1", "remote", "10.0.0.2"),
            t("tunnel", "add", "eth0", "mode", "ipip", "local", "10.0.0.1", "remote", "10.0.0.2"),
            t("tunnel", "add", "tnl-x", "mode", "ipip", "local", "10.0.0.1", "remote", "10.0.0.2;id"),
            t("tunnel", "del", "eth0"),
            t("route", "replace", "10.10.100.0/24", "dev", "tnl-mn"),          # whole network, not a /32
            t("route", "replace", "10.10.100.2/32", "dev", "eth-ana"),         # not a tunnel device
            t("route", "del", "default"),
            t("addr", "del", "10.10.2.1/24", "dev", "eth-ana"),
            t("addr", "del", "10.10.100.1/32", "dev", "eth-ana"),
            ("ip", "tunnel", "add", "tnl-mn", "mode", "ipip", "local", "1.1.1.1", "remote", "2.2.2.2"),   # host namespace
            ("ip", "-n", "othernet", "tunnel", "del", "tnl-mn"),
        ]
        for argv in bad:
            with self.subTest(argv=argv), self.assertRaises(SafetyError):
                validate_argv(argv, P)


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.req, self.ack = d / "req.jsonl", d / "ack.jsonl"
        self.host = FakeHost(namespaces={"nsl-ha", "nsl-mn", "nsl-cn"})
        runner = Runner(P, dry_run=False, use_sudo=False, executor=self.host)
        self.applier = LinuxDataPlaneApplier(runner, C, ["10.10.4.2", "10.10.5.2"])
        self.ctrl = dp.DataPlaneController(self.req, self.ack, self.applier.apply).start()
        self.addCleanup(self.ctrl.stop)

    def mutating(self):
        return [c for c in self.host.calls if c[1] == "-n"]

    def test_install_move_remove_sequence(self):
        ha = dp.HandshakeHaDataPlane(self.req, self.ack, timeout_s=3)
        ha.install_binding("10.10.100.2", "10.10.4.2")
        ha.install_binding("10.10.100.2", "10.10.5.2")                   # movement: change, not add
        ha.remove_binding("10.10.100.2")
        verbs = [(c[3], c[4]) for c in self.mutating()]
        self.assertEqual(verbs, [("tunnel", "add"), ("link", "set"), ("route", "replace"), ("tunnel", "change"),
                                 ("route", "replace"), ("route", "del"), ("tunnel", "del")])
        self.assertIn("remote", self.mutating()[3])
        self.assertEqual(self.mutating()[3][-1], "10.10.5.2")

    def test_mn_side(self):
        mn = dp.HandshakeMnDataPlane(self.req, self.ack, timeout_s=3)
        mn.set_care_of("10.10.4.2")
        mn.set_care_of("10.10.5.2")
        mn.clear()
        self.assertEqual([(c[3], c[4]) for c in self.mutating()],
                         [("tunnel", "add"), ("link", "set"), ("tunnel", "change"), ("tunnel", "del")])

    def test_care_of_address_outside_the_testbed_is_refused(self):
        ha = dp.HandshakeHaDataPlane(self.req, self.ack, timeout_s=3)
        with self.assertRaises(dp.DataPlaneError) as cm:
            ha.install_binding("10.10.100.2", "8.8.8.8")
        self.assertIn("not a testbed address", str(cm.exception))
        self.assertEqual(self.mutating(), [])                            # nothing executed

    def test_foreign_home_address_and_unknown_op_refused(self):
        ha = dp.HandshakeHaDataPlane(self.req, self.ack, timeout_s=3)
        with self.assertRaises(dp.DataPlaneError):
            ha.install_binding("10.10.100.99", "10.10.4.2")
        with self.assertRaises(dp.DataPlaneError):
            ha._call("rm_rf", path="/")
        self.assertEqual(self.mutating(), [])

    def test_failing_command_is_reported_to_the_agent(self):
        self.host.fail_on_call = len(self.host.calls) + 1
        ha = dp.HandshakeHaDataPlane(self.req, self.ack, timeout_s=3)
        with self.assertRaises(dp.DataPlaneError):
            ha.install_binding("10.10.100.2", "10.10.4.2")

    def test_missing_controller_times_out(self):
        self.ctrl.stop()
        ha = dp.HandshakeHaDataPlane(self.req, self.ack, timeout_s=0.2)
        with self.assertRaises(dp.DataPlaneError) as cm:
            ha.install_binding("10.10.100.2", "10.10.4.2")
        self.assertIn("did not acknowledge", str(cm.exception))

    def test_cleanup_removes_what_was_created_and_is_idempotent(self):
        dp.HandshakeHaDataPlane(self.req, self.ack).install_binding("10.10.100.2", "10.10.4.2")
        dp.HandshakeMnDataPlane(self.req, self.ack).set_care_of("10.10.4.2")
        self.applier.cleanup()
        self.applier.cleanup()
        tail = [(c[2], c[3], c[4]) for c in self.mutating()][-4:]
        self.assertEqual(sorted(t for t in tail if t[1:] == ("tunnel", "del")), [("nsl-ha", "tunnel", "del"), ("nsl-mn", "tunnel", "del")])
        self.assertEqual(sum(1 for c in self.mutating() if c[3:5] == ("tunnel", "del")), 2)   # second cleanup: no-op


if __name__ == "__main__":
    unittest.main()
