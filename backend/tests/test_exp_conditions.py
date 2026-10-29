"""Stage 7: network conditions, seeded loss in the emulator, spec round trip, backwards compatibility with Stage 5 results."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.compare.experiment import ROOT, ExperimentSpec
from app.compare.runner import check, run_experiment
from app.mobileip.simnet import Sim, SimLink
from app.netem.conditions import NetworkConditions, apply_conditions, direct_path_rtt_ms
from app.netem.topology_config import load_config

TOPO = ROOT / "config" / "topology_ro.yaml"


def delays(cfg):
    return {l.name: l.effective_netem(l.a).delay_ms for l in cfg.links}


class Apply(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(TOPO)

    def test_default_changes_nothing(self):
        self.assertEqual(apply_conditions(self.cfg, NetworkConditions()), self.cfg)
        self.assertTrue(NetworkConditions(seed=5).is_default())

    def test_input_is_not_modified(self):
        before = delays(self.cfg)
        apply_conditions(self.cfg, NetworkConditions(latency_scale=3, ha_distance_ms=40, loss_percent=2))
        self.assertEqual(delays(self.cfg), before)

    def test_latency_scale(self):
        d0, d = delays(self.cfg), delays(apply_conditions(self.cfg, NetworkConditions(latency_scale=2.5)))
        for k in d0:
            self.assertAlmostEqual(d[k], d0[k] * 2.5)

    def test_ha_distance_only_touches_links_at_the_home_agent(self):
        d0, d = delays(self.cfg), delays(apply_conditions(self.cfg, NetworkConditions(ha_distance_ms=100)))
        for k in ("cn-ha", "ha-ana", "ha-anb"):
            self.assertAlmostEqual(d[k], d0[k] + 100)
        for k in ("ana-mn", "anb-mn", "cn-ana", "cn-anb"):
            self.assertEqual(d[k], d0[k])

    def test_override_then_scale_then_ha(self):
        c = NetworkConditions(link_delay_ms={"cn-ha": 10}, latency_scale=2, ha_distance_ms=7)
        self.assertAlmostEqual(delays(apply_conditions(self.cfg, c))["cn-ha"], 10 * 2 + 7)

    def test_loss_is_applied_only_to_the_named_links_in_both_directions(self):
        cfg = apply_conditions(self.cfg, NetworkConditions(loss_percent=3))
        for l in cfg.links:
            want = 3.0 if l.name in ("ana-mn", "anb-mn") else 0.0
            self.assertEqual((l.effective_netem(l.a).loss_percent, l.effective_netem(l.b).loss_percent), (want, want))
        cfg = apply_conditions(self.cfg, NetworkConditions(loss_percent=1, loss_links=("cn-ha",)))
        self.assertEqual({l.name for l in cfg.links if l.effective_netem(l.a).loss_percent}, {"cn-ha"})

    def test_other_netem_fields_are_preserved(self):
        l = next(l for l in apply_conditions(self.cfg, NetworkConditions(latency_scale=2)).links if l.name == "anb-mn")
        self.assertEqual((l.netem.jitter_ms, l.netem.rate_mbit), (2.0, 10.0))

    def test_validation(self):
        bad = [NetworkConditions(latency_scale=0), NetworkConditions(loss_percent=100), NetworkConditions(loss_percent=-1),
               NetworkConditions(ha_distance_ms=-1), NetworkConditions(link_delay_ms={"nope": 1}), NetworkConditions(loss_links=("nope",)),
               NetworkConditions(registration_loss_percent=150), NetworkConditions(latency_scale=1e6)]
        for c in bad:
            self.assertIsNotNone(c.validate(self.cfg), c)
        self.assertIsNone(NetworkConditions(loss_percent=5, ha_distance_ms=30).validate(self.cfg))

    def test_direct_path_reference_rtt(self):
        self.assertEqual(direct_path_rtt_ms(self.cfg, "eth-a"), 2 * (8 + 5))
        self.assertEqual(direct_path_rtt_ms(self.cfg, "eth-b"), 2 * (12 + 15))
        scaled = apply_conditions(self.cfg, NetworkConditions(latency_scale=2, ha_distance_ms=500))
        self.assertEqual(direct_path_rtt_ms(scaled, "eth-b"), 2 * 2 * (12 + 15))      # HA distance does not enter the direct path
        self.assertIsNone(direct_path_rtt_ms(self.cfg, "eth-zzz"))

    def test_json_round_trip_and_unknown_fields(self):
        c = NetworkConditions(latency_scale=2, link_delay_ms={"cn-ha": 3}, loss_percent=1.5, seed=9)
        self.assertEqual(NetworkConditions.from_json(json.loads(json.dumps(c.to_json()))), c)
        with self.assertRaises(ValueError):
            NetworkConditions.from_json({"bogus": 1})


class SimLoss(unittest.TestCase):
    def _link(self, p, seed=1):
        return SimLink("l", ("a", "x"), ("b", "y"), 1000, loss_a_to_b=p, loss_b_to_a=0.0, seed=seed)

    def test_zero_loss_draws_nothing(self):
        l = self._link(0.0)
        self.assertFalse(any(l.lose(True) for _ in range(1000)))
        self.assertEqual(l._rng, {})

    def test_rate_is_close_to_nominal_and_directions_are_independent(self):
        l = self._link(0.2)
        n = 20000
        self.assertAlmostEqual(sum(l.lose(True) for _ in range(n)) / n, 0.2, delta=0.01)
        self.assertEqual(sum(l.lose(False) for _ in range(n)), 0)                 # reverse direction has p = 0

    def test_same_seed_same_sequence_different_seed_different(self):
        self.assertEqual(self._seq(5), self._seq(5))
        self.assertNotEqual(self._seq(5), self._seq(6))

    def _seq(self, seed):
        l = self._link(0.3, seed)
        return [l.lose(True) for _ in range(200)]


class EndToEnd(unittest.TestCase):
    def run_spec(self, spec):
        with tempfile.TemporaryDirectory() as d:
            res = run_experiment(spec, d)
            files = {p.name: p.read_bytes() for p in res.run_dir.iterdir() if p.is_file()}
            return res, files

    def test_loss_changes_results_but_is_seed_reproducible(self):
        mk = lambda seed, loss: ExperimentSpec("mip-basic", conditions=NetworkConditions(loss_percent=loss, seed=seed), seed=seed)
        r0, f0 = self.run_spec(mk(1, 0))
        r1, f1 = self.run_spec(mk(1, 3))
        r1b, f1b = self.run_spec(mk(1, 3))
        r2, f2 = self.run_spec(mk(2, 3))
        self.assertGreater(r1.metrics["loss"]["lost"], r0.metrics["loss"]["lost"])
        self.assertEqual(f1["comparison_metrics.json"], f1b["comparison_metrics.json"])
        for n in ("sent.csv", "echoes.csv", "received.csv", "events.csv"):
            self.assertEqual(f1[n], f1b[n], n)
        self.assertNotEqual(f1["echoes.csv"], f2["echoes.csv"])

    def test_ha_distance_raises_mobile_ip_rtt_but_not_tcp(self):
        run = lambda a, ha: run_experiment(ExperimentSpec(a, conditions=NetworkConditions(ha_distance_ms=ha)), tempfile.mkdtemp())
        mip0, mip1 = run("mip-basic", 0), run("mip-basic", 100)
        self.assertGreater(mip1.metrics["handoffs"][0]["rtt_after_ms"], mip0.metrics["handoffs"][0]["rtt_after_ms"] + 150)
        t0, t1 = run("tcp-reconnect", 0), run("tcp-reconnect", 100)
        self.assertEqual(t0.metrics["handoffs"][0]["rtt_after_ms"], t1.metrics["handoffs"][0]["rtt_after_ms"])
        exp = json.loads((t1.run_dir / "experiment.json").read_text())
        self.assertIn("not applicable", exp["condition_applicability"]["ha_distance_ms"])

    def test_registration_loss_is_injected_and_recorded(self):
        res, f = self.run_spec(ExperimentSpec("mip-basic", conditions=NetworkConditions(registration_loss_percent=60, seed=3)))
        drops = json.loads(f["mip_summary.json"])["drops"]
        self.assertTrue(any("registration_loss_injected" in k for k in drops), drops)

    def test_invalid_conditions_are_refused_before_running(self):
        st, why = check(ExperimentSpec("mip-basic", conditions=NetworkConditions(loss_links=("nope",))))
        self.assertEqual(st, "invalid")
        self.assertIn("unknown link", why)

    def test_experiment_json_records_software_conditions_and_effective_links(self):
        res, f = self.run_spec(ExperimentSpec("mip-basic", seed=4, conditions=NetworkConditions(loss_percent=2, latency_scale=2, seed=4)))
        e = json.loads(f["experiment.json"])
        self.assertEqual(e["spec"]["conditions"]["loss_percent"], 2)
        self.assertEqual(e["spec"]["seed"], 4)
        for k in ("python", "numpy", "scipy", "netsim_lab", "framework", "platform"):
            self.assertIn(k, e["software"])
        link = next(x for x in e["effective_links"] if x["link"] == "ana-mn")
        self.assertEqual((link["delay_ms"], link["loss_percent_a_to_b"]), (10.0, 2.0))
        self.assertEqual(ExperimentSpec.from_json(e["spec"]), ExperimentSpec("mip-basic", seed=4, conditions=NetworkConditions(
            loss_percent=2, latency_scale=2, seed=4)))

    def test_spec_from_json_accepts_stage5_files_and_rejects_unknown_fields(self):
        old = json.loads((ROOT / "examples/comparison/mip-basic-emulated/run-1/experiment.json").read_text())["spec"]
        self.assertTrue(ExperimentSpec.from_json(old).conditions.is_default())
        with self.assertRaises(ValueError):
            ExperimentSpec.from_json(dict(old, surprise=1))


class BackwardsCompatibility(unittest.TestCase):
    """Default conditions must reproduce the Stage 5 example results exactly (these three approaches are unaffected by the TcpLite fix)."""

    def test_stage5_examples_are_reproduced_byte_for_byte(self):
        cases = {"mip-basic-emulated": ExperimentSpec("mip-basic"), "ro-model-emulated": ExperimentSpec("ro-model"),
                 "tcp-reconnect-emulated-notify": ExperimentSpec("tcp-reconnect", variant="notify")}
        for scen, spec in cases.items():
            with tempfile.TemporaryDirectory() as d:
                res = run_experiment(spec, d)
                old = ROOT / "examples" / "comparison" / scen / "run-1"
                for n in ("sent.csv", "echoes.csv", "received.csv", "events.csv", "comparison_metrics.json"):
                    self.assertEqual(hashlib.sha256((res.run_dir / n).read_bytes()).hexdigest(),
                                     hashlib.sha256((old / n).read_bytes()).hexdigest(), f"{scen}/{n}")


if __name__ == "__main__":
    unittest.main()
