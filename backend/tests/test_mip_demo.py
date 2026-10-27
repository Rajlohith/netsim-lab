"""Live-demo orchestrator logic with a fake host and fake agent processes (no root, no ip, no kernel tunnels)."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from app.mobileip import dataplane as dp
from app.mobileip.demo import DemoError, DemoSpec, run_demo
from app.netem.runner import Runner
from app.netem.topology import TopologyManager
from app.netem.topology_config import load_config
from app.traffic.loader import read_csv
from tests.netem_fakes import FakeHost

ROOT = Path(__file__).resolve().parents[2]
P = "nsl-"


def opt(args, name):
    return args[args.index(name) + 1]


class FakeProc:
    def __init__(self, stop_file=None, finish_after=None):
        self.stop_file, self.finish_after, self.t0 = stop_file, finish_after, time.monotonic()

    def poll(self):
        if self.finish_after is not None and time.monotonic() - self.t0 >= self.finish_after:
            return 0
        if self.stop_file is not None and Path(self.stop_file).exists():
            return 0
        return None

    @property
    def returncode(self):
        return self.poll()


class FakeAgents:
    """Stands in for the HA daemon / MN agent / probe tools; uses the REAL handshake classes."""

    def __init__(self, refuse_coa=None):
        self.refuse_coa, self.ha_paths, self.launched, self.threads = refuse_coa, None, [], []

    def popen(self, argv, **kw):
        self.launched.append(list(argv))
        i = argv.index("-m")
        module, args = argv[i + 1], argv[i + 2:]
        if module == "app.mobileip.runtime" and args[0] == "ha":
            self.ha_paths = (opt(args, "--dp-req"), opt(args, "--dp-ack"))
            Path(opt(args, "--ready-file")).write_text("4340\n")
            return FakeProc(stop_file=opt(args, "--stop-file"))
        if module == "app.mobileip.runtime":
            return self._mn(args)
        if module == "app.traffic.receiver":
            Path(opt(args, "--ready-file")).write_text("5005\n")
            return FakeProc(stop_file=opt(args, "--stop-file"))
        return FakeProc(finish_after=0.35)                            # sender

    def _mn(self, args):
        Path(opt(args, "--ready-file")).write_text("ready\n")
        ctl, status, stop = Path(opt(args, "--control-file")), Path(opt(args, "--status-file")), Path(opt(args, "--stop-file"))
        mn_dp = dp.HandshakeMnDataPlane(opt(args, "--dp-req"), opt(args, "--dp-ack"), timeout_s=3)
        ha_dp = dp.HandshakeHaDataPlane(*self.ha_paths, timeout_s=3)

        def loop():
            off = 0
            while not stop.exists():
                if ctl.exists():
                    data = ctl.read_text()[off:]
                    off += len(data)
                    for line in data.splitlines():
                        cmd = json.loads(line)
                        if cmd["op"] == "attach":
                            mn_dp.set_care_of(cmd["coa"])
                            if cmd["coa"] == self.refuse_coa:
                                status.write_text(json.dumps({"state": "failed", "care_of": cmd["coa"]}))
                                continue
                            ha_dp.install_binding("10.10.100.2", cmd["coa"])
                            status.write_text(json.dumps({"state": "registered", "care_of": cmd["coa"]}))
                        else:
                            ha_dp.remove_binding("10.10.100.2")
                            mn_dp.clear()
                            status.write_text(json.dumps({"state": "detached", "care_of": ""}))
                time.sleep(0.005)
        th = threading.Thread(target=loop, daemon=True)
        th.start()
        self.threads.append(th)
        return FakeProc(stop_file=stop)


class DemoOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = FakeHost()

    def mgr(self, agents):
        runner = Runner(P, dry_run=False, use_sudo=False, executor=self.host, popen_factory=agents.popen)
        m = TopologyManager(load_config(ROOT / "config" / "topology.yaml"), runner, P, None)
        m.create()
        self.host.calls.clear()
        return m

    def run_it(self, agents, **kw):
        spec = DemoSpec(duration_s=0.6, rate_pps=20, moves=((0.15, "eth-b"), (0.4, "eth-a")))
        with mock.patch("app.mobileip.demo.os.getuid", return_value=1000), \
             mock.patch("app.mobileip.demo.os.getgid", return_value=1000):
            return run_demo(spec, self.mgr(agents), Path(self.tmp.name), run_id="run1", startup_allowance_s=0.05,
                            poll_s=0.005, reg_timeout_s=kw.get("reg_timeout_s", 3.0))

    def cmds(self):
        return [c for c in self.host.calls if c[1] == "-n"]

    def index(self, pred):
        return next(i for i, c in enumerate(self.cmds()) if pred(c))

    def test_full_sequence_and_cleanup(self):
        agents = FakeAgents()
        run_dir, metrics = self.run_it(agents)
        cs = self.cmds()
        # home addresses exist before any tunnel; MN starts with only eth-a
        self.assertLess(self.index(lambda c: c[3:5] == ("addr", "add")), self.index(lambda c: c[3:5] == ("tunnel", "add")))
        self.assertLess(self.index(lambda c: c[3:7] == ("link", "set", "eth-b", "down")),
                        self.index(lambda c: c[3:5] == ("tunnel", "add")))
        # HA tunnel: created once for the first binding, then re-pointed on each movement
        ha_tunnel = [c[4] for c in cs if c[2] == "nsl-ha" and c[3] == "tunnel"]
        self.assertEqual(ha_tunnel[:3], ["add", "change", "change"])
        self.assertEqual(ha_tunnel[-1], "del")
        remotes = [c[-1] for c in cs if c[2] == "nsl-ha" and c[3] == "tunnel" and c[4] in ("add", "change")]
        self.assertEqual(remotes, ["10.10.4.2", "10.10.5.2", "10.10.4.2"])       # reachable location follows the moves
        # make-before-break ordering of the emulated movement
        up_b = self.index(lambda c: c[2] == "nsl-mn" and c[3:7] == ("link", "set", "eth-b", "up"))
        down_a = self.index(lambda c: c[2] == "nsl-mn" and c[3:7] == ("link", "set", "eth-a", "down"))
        self.assertLess(up_b, down_a)
        # cleanup: tunnels gone on both sides, home addresses removed, every MN link restored
        for ns in ("nsl-ha", "nsl-mn"):
            self.assertEqual([c[4] for c in cs if c[2] == ns and c[3] == "tunnel"][-1], "del")
        self.assertEqual(sum(1 for c in cs if c[3:5] == ("addr", "del")), 2)
        final = {}
        for c in cs:
            if c[2] == "nsl-mn" and c[3:5] == ("link", "set") and c[5] in ("eth-a", "eth-b"):
                final[c[5]] = c[6]
        self.assertEqual(final, {"eth-a": "up", "eth-b": "up"})

    def test_records_events_and_launches_unprivileged(self):
        agents = FakeAgents()
        run_dir, metrics = self.run_it(agents)
        ev = read_csv(run_dir / "events.csv", "events.csv").rows
        self.assertEqual([(e["event_id"], e["kind"]) for e in ev],
                         [("scenario", "scenario_start"), ("handoff-1", "handoff_start"), ("handoff-1", "handoff_end"),
                          ("handoff-2", "handoff_start"), ("handoff-2", "handoff_end"), ("scenario", "scenario_end")])
        self.assertEqual(ev[-1]["detail"], "ok")
        self.assertEqual(ev[1]["detail"], "eth-a -> eth-b")
        mip = (run_dir / "mip_events.orch.csv").read_text()
        self.assertEqual(mip.count("link_change"), 2)
        self.assertEqual(len(agents.launched), 4)                          # HA, MN agent, probe receiver, probe sender
        for argv in agents.launched:
            self.assertIn("setpriv", argv)
            self.assertIn("--reuid=1000", argv)
        names = sorted(a[a.index("-m") + 1] for a in agents.launched)
        self.assertEqual(names, ["app.mobileip.runtime"] * 2 + ["app.traffic.receiver", "app.traffic.sender"])
        key = run_dir / "mip.key"
        self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")
        for argv in agents.launched:                                       # the secret never appears on a command line
            self.assertNotIn(key.read_text().strip(), " ".join(argv))
        self.assertEqual(metrics["mode"], "live")
        self.assertFalse(metrics["tunnel"]["measured"])                    # live overhead is analytic, and says so
        self.assertIn("analytic", metrics["tunnel"]["basis"])

    def test_failed_registration_is_reported_and_everything_is_cleaned_up(self):
        agents = FakeAgents(refuse_coa="10.10.5.2")
        with self.assertRaises(DemoError) as cm:
            self.run_it(agents, reg_timeout_s=0.4)
        self.assertIn("partial data kept", str(cm.exception))
        run_dir = Path(self.tmp.name) / "mip-live-demo" / "run1"
        ev = read_csv(run_dir / "events.csv", "events.csv").rows
        self.assertEqual(ev[-1]["kind"], "scenario_end")
        self.assertIn("registration on eth-b", ev[-1]["detail"])
        self.assertIn("registration failed", [e["detail"] for e in ev if e["kind"] == "handoff_end"])
        cs = self.cmds()
        self.assertEqual([c[4] for c in cs if c[2] == "nsl-ha" and c[3] == "tunnel"][-1], "del")
        self.assertEqual([c[4] for c in cs if c[2] == "nsl-mn" and c[3] == "tunnel"][-1], "del")
        self.assertEqual(sum(1 for c in cs if c[3:5] == ("addr", "del")), 2)
        self.assertEqual({c[5]: c[6] for c in cs if c[2] == "nsl-mn" and c[3:5] == ("link", "set") and c[5].startswith("eth-")},
                         {"eth-a": "up", "eth-b": "up"})

    def test_make_before_break_registers_before_releasing_the_old_link(self):
        agents = FakeAgents()
        spec = DemoSpec(duration_s=0.6, rate_pps=20, moves=((0.15, "eth-b"), (0.4, "eth-a")), make_before_break=True,
                        old_link_hold_ms=50)
        with mock.patch("app.mobileip.demo.os.getuid", return_value=1000), \
             mock.patch("app.mobileip.demo.os.getgid", return_value=1000):
            run_dir, metrics = run_demo(spec, self.mgr(agents), Path(self.tmp.name), run_id="mbb", startup_allowance_s=0.05,
                                        poll_s=0.005, reg_timeout_s=3.0)
        cs = self.cmds()
        up_b = self.index(lambda c: c[2] == "nsl-mn" and c[3:7] == ("link", "set", "eth-b", "up"))
        ha_to_b = self.index(lambda c: c[2] == "nsl-ha" and c[3] == "tunnel" and c[-1] == "10.10.5.2")
        down_a = self.index(lambda c: c[2] == "nsl-mn" and c[3:7] == ("link", "set", "eth-a", "down"))
        self.assertLess(up_b, ha_to_b)                       # new link first,
        self.assertLess(ha_to_b, down_a)                     # binding moved to the new care-of address BEFORE the old link goes
        ev = read_csv(run_dir / "events.csv", "events.csv").rows
        end1 = next(e for e in ev if e["event_id"] == "handoff-1" and e["kind"] == "handoff_end")
        self.assertEqual(end1["detail"], "registered")
        self.assertEqual(ev[-1]["detail"], "ok")

    def test_unknown_interface_is_rejected_before_touching_anything(self):
        agents = FakeAgents()
        spec = DemoSpec(moves=((1.0, "eth0"),))
        with mock.patch("app.mobileip.demo.os.getuid", return_value=1000):
            with self.assertRaises(DemoError):
                run_demo(spec, self.mgr(agents), Path(self.tmp.name), run_id="run2")
        self.assertEqual(self.host.calls, [])


if __name__ == "__main__":
    unittest.main()
