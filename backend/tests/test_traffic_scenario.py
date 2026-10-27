"""Handoff orchestrator logic with a fake host (no root/ip needed)."""
import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.netem import plan
from app.netem.errors import SafetyError
from app.netem.runner import Runner
from app.netem.safety import validate_argv
from app.netem.topology import TopologyManager
from app.netem.topology_config import load_config
from app.traffic.analyze import analyze_run
from app.traffic.loader import read_csv
from app.traffic.scenario import (HandoffAction, ScenarioError, ScenarioSpec, load_scenario, run_scenario,
                                  scenario_main)
from tests.netem_fakes import FakeHost

ROOT = Path(__file__).resolve().parents[2]
P = "nsl-"


class FakeProc:
    def __init__(self, argv, fail_after=None):
        self.argv, self.rc = argv, 3 if fail_after is not None else 0
        self.fail_after, self.t0 = fail_after, time.monotonic()
        if "--ready-file" in argv:
            Path(argv[argv.index("--ready-file") + 1]).write_text("5005\n")

    @property
    def returncode(self):
        return self.poll()

    def poll(self):
        if self.fail_after is not None and time.monotonic() - self.t0 < self.fail_after:
            return None
        return self.rc


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = FakeHost()
        self.launched = []

    def mgr(self, sender_fail_after=None):
        def popen(argv, **kw):
            self.launched.append(list(argv))
            is_sender = "app.traffic.sender" in argv
            return FakeProc(argv, sender_fail_after if is_sender else None)
        runner = Runner(P, dry_run=False, use_sudo=False, executor=self.host, popen_factory=popen)
        m = TopologyManager(load_config(ROOT / "config" / "topology.yaml"), runner, P, None)
        m.create()
        return m

    def run_spec(self, mgr, spec):
        with mock.patch("app.traffic.scenario.os.getuid", return_value=1000), \
             mock.patch("app.traffic.scenario.os.getgid", return_value=1000):
            return run_scenario(spec, mgr, Path(self.tmp.name), run_id="run1", startup_allowance_s=0.05, poll_s=0.01)

    def test_handoff_events_commands_and_ordering(self):
        spec = ScenarioSpec("unit-handoff", duration_s=0.5, handoffs=(
            HandoffAction(0.1, "mn", "eth-a", "down"), HandoffAction(0.3, "mn", "eth-a", "up")))
        run_dir, metrics = self.run_spec(self.mgr(), spec)
        links = [c for c in self.host.calls if c[1] == "-n" and c[2] == "nsl-mn" and c[3:5] == ("link", "set")]
        self.assertEqual([c[-1] for c in links if c[5] == "eth-a" and c[-1] in ("up", "down")][-2:], ["down", "up"])
        ev = read_csv(run_dir / "events.csv", "events.csv").rows
        kinds = [(e["event_id"], e["kind"]) for e in ev]
        self.assertEqual(kinds, [("scenario", "scenario_start"), ("handoff-1", "handoff_start"),
                                 ("handoff-1", "handoff_end"), ("handoff-2", "handoff_start"),
                                 ("handoff-2", "handoff_end"), ("scenario", "scenario_end")])
        monos = [e["mono_ns"] for e in ev]
        self.assertEqual(monos, sorted(monos))
        self.assertGreaterEqual(ev[3]["mono_ns"] - ev[1]["mono_ns"], 0.19e9)        # 0.3 s - 0.1 s, give or take
        self.assertEqual(metrics["run_status"], "incomplete")                       # fake tools wrote no data
        self.assertTrue((run_dir / "scenario.json").exists())

    def test_launch_commands_are_allow_listed_and_unprivileged(self):
        spec = ScenarioSpec("unit-launch", duration_s=0.2)
        self.run_spec(self.mgr(), spec)
        self.assertEqual(len(self.launched), 2)
        for argv in self.launched:
            validate_argv(argv, P)
            self.assertIn("--reuid=1000", argv)
            self.assertIn("setpriv", argv)
        rx = next(a for a in self.launched if "app.traffic.receiver" in a)
        tx = next(a for a in self.launched if "app.traffic.sender" in a)
        self.assertEqual(rx[3], "nsl-cn")
        self.assertEqual(tx[3], "nsl-mn")
        self.assertIn("10.10.1.2:5005", tx)
        self.assertIn("--start-at-mono-ns", tx)

    def test_failure_restores_interface_and_keeps_partial_data(self):
        spec = ScenarioSpec("unit-fail", duration_s=1.0, handoffs=(
            HandoffAction(0.05, "mn", "eth-a", "down"), HandoffAction(0.4, "mn", "eth-a", "up")))
        with self.assertRaises(ScenarioError) as cm:
            self.run_spec(self.mgr(sender_fail_after=0.2), spec)
        self.assertIn("partial data kept", str(cm.exception))
        sets = [c[-1] for c in self.host.calls if c[1:3] == ("-n", "nsl-mn") and c[3:6] == ("link", "set", "eth-a")]
        self.assertEqual(sets[-2:], ["down", "up"])                                 # restored in cleanup
        run_dir = Path(self.tmp.name) / "unit-fail" / "run1"
        ev = read_csv(run_dir / "events.csv", "events.csv").rows
        self.assertEqual(ev[-1]["kind"], "scenario_end")
        self.assertNotEqual(ev[-1]["detail"], "ok")

    def test_spec_loading_and_validation(self):
        s = load_scenario(ROOT / "config" / "scenarios" / "handoff_a_to_b.yaml")
        self.assertEqual((s.scenario_id, len(s.handoffs), s.handoffs[0].state), ("handoff-a-to-b", 2, "down"))
        bad = Path(self.tmp.name) / "bad.yaml"
        for text in ("scenario_id: x\nfoo: 1\n", "scenario_id: ../x\n",
                     "scenario_id: x\ntraffic: {duration_s: 5}\nhandoffs: [{at_s: 9, node: mn, ifname: eth-a, state: down}]\n",
                     "scenario_id: x\nhandoffs: [{at_s: 1, node: mn, ifname: eth-a, state: sideways}]\n"):
            bad.write_text(text)
            with self.subTest(text=text), self.assertRaises(ScenarioError):
                load_scenario(bad)

    def test_unknown_interface_is_refused(self):
        mgr = self.mgr()
        from app.netem.errors import TopologyError
        with self.assertRaises(TopologyError):
            mgr.set_interface("mn", "eth0", False)

    def test_dry_run_prints_plan_and_changes_nothing(self):
        out = io.StringIO()
        args = SimpleNamespace(spec=ROOT / "config" / "scenarios" / "handoff_a_to_b.yaml", topology=None,
                               out_dir=Path(self.tmp.name), prefix=P, execute=False)
        with mock.patch("app.traffic.scenario.os.geteuid", return_value=1000), redirect_stdout(out):
            rc = scenario_main(args)
        self.assertEqual(rc, 0)
        self.assertIn("ip -n nsl-mn link set eth-a down", out.getvalue())
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


class LaunchSafetyTests(unittest.TestCase):
    def cmd(self, **kw):
        a = dict(ns="nsl-cn", uid=1000, gid=1000, python="/usr/bin/python3", backend_dir="/home/u/netsim/backend",
                 module="app.traffic.receiver", args=["--port", "5005"])
        a.update(kw)
        return plan.launch_cmd(a["ns"], a["uid"], a["gid"], a["python"], a["backend_dir"], a["module"], a["args"])

    def test_valid_launch(self):
        validate_argv(self.cmd().argv, P)

    def test_rejections(self):
        bad = [self.cmd(uid=0), self.cmd(module="app.netem"), self.cmd(module="os"),
               self.cmd(python="python3"), self.cmd(backend_dir="relative/dir"),
               self.cmd(args=["--port", "5005; id"]), self.cmd(args=["$(id)"]), self.cmd(ns="default"),
               self.cmd(args=["--out-dir", "/tmp/has space"])]
        for c in bad:
            with self.subTest(argv=c.argv[3:]), self.assertRaises(SafetyError):
                validate_argv(c.argv, P)

    def test_link_down_rules(self):
        validate_argv(plan.link_set_cmd("nsl-mn", "eth-a", False).argv, P)
        validate_argv(plan.link_set_cmd("nsl-mn", "eth-a", True).argv, P)
        with self.assertRaises(SafetyError):
            validate_argv(("ip", "-n", "nsl-mn", "link", "set", "lo", "down"), P)
        with self.assertRaises(SafetyError):
            validate_argv(("ip", "link", "set", "eth0", "down"), P)
        with self.assertRaises(SafetyError):
            validate_argv(("ip", "-n", "othernet", "link", "set", "eth-a", "down"), P)


if __name__ == "__main__":
    unittest.main()
