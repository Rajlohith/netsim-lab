import os
import unittest

from app.services.catalog import Catalog, load_scenarios
from app.services.errors import ConfigurationError
from app.services.model_registry import ModelRegistry
from app.services.settings import ServiceSettings
from app.services.validation import ALLOWED_FIELDS, validate_request

S = ServiceSettings().validate()
C, M = Catalog(S), ModelRegistry(S)


def v(raw, s=S):
    return validate_request(raw, s, C, M)


def codes(raw, s=S):
    return [(i.field, i.code) for i in v(raw, s)[0]]


class Catalogue(unittest.TestCase):
    def test_approaches_come_from_registry_and_report_availability(self):
        a = {x["id"]: x for x in C.approaches()}
        self.assertEqual(set(a), {"tcp-reconnect", "mip-basic", "mip-mbb", "ro-model", "quic-migration"})
        self.assertEqual(a["mip-basic"]["environments"]["live"]["check"], "not_exposed")
        self.assertFalse(a["mip-basic"]["environments"]["live"]["exposed_via_api"])
        self.assertTrue(a["mip-basic"]["environments"]["emulated"]["runnable_here"])
        self.assertTrue(a["mip-mbb"]["supports_make_before_break"] and not a["mip-basic"]["supports_make_before_break"])
        q = a["quic-migration"]["environments"]["emulated"]
        try:
            import aioquic  # noqa: F401
        except ImportError:
            self.assertFalse(q["runnable_here"]); self.assertEqual(q["check"], "unavailable")

    def test_every_shipped_scenario_is_valid_for_a_home_agent_approach(self):
        for sid in C.scenario_ids():
            self.assertEqual(v({"approach": "mip-basic", "scenario": sid})[0], [], sid)

    def test_scenarios_list_resolved_parameters(self):
        sc = {s["id"]: s for s in C.scenarios()}
        self.assertEqual(sc["baseline"]["parameters"]["duration_s"], 10.0)
        self.assertEqual(sc["high-latency"]["parameters"]["conditions"]["latency_scale"], 2.0)
        self.assertEqual(sc["rapid-second-handoff"]["parameters"]["moves"][1], {"at_s": 5.5, "interface": "eth-a"})
        self.assertTrue(sc["distant-home-agent"]["requires_home_agent"])
        self.assertNotIn("tcp-reconnect", sc["distant-home-agent"]["effective_for_approaches"])

    def test_bad_scenario_file_is_rejected(self):
        import tempfile, pathlib
        p = pathlib.Path(tempfile.mkdtemp()) / "s.yaml"
        p.write_text("scenarios:\n  - id: a\n    parameters: {topology: /etc/passwd}\n")
        with self.assertRaises(ConfigurationError):
            load_scenarios(p)

    def test_capabilities_say_polling_not_websocket(self):
        c = C.capabilities(model_ids=M.ids())
        self.assertFalse(c["live_updates"]["websocket"]); self.assertTrue(c["live_updates"]["polling"])
        self.assertFalse(c["live_updates"]["live_metrics_available"]); self.assertIn("live", c["environments"]["never_exposed"])


class Validation(unittest.TestCase):
    def test_minimal_request_resolves_with_defaults(self):
        issues, r = v({"approach": "mip-basic"})
        self.assertEqual(issues, [])
        self.assertEqual((r.scenario, r.environment, r.spec.duration_s, r.spec.seed), ("baseline", "emulated", 10.0, 0))
        self.assertEqual(r.spec.topology.name, "topology_ro.yaml")

    def test_explicit_fields_override_scenario(self):
        _, r = v({"approach": "mip-basic", "scenario": "high-latency", "conditions": {"loss_percent": 1}, "seed": 5})
        self.assertEqual((r.spec.conditions.latency_scale, r.spec.conditions.loss_percent, r.spec.conditions.seed), (2.0, 1, 5))
        _, r = v({"approach": "mip-basic", "scenario": "high-latency", "conditions": {"latency_scale": 3}})
        self.assertEqual(r.spec.conditions.latency_scale, 3)

    def test_unknown_fields_are_errors_never_ignored(self):
        for raw in ({"approach": "mip-basic", "topology": "/etc/passwd"}, {"approach": "mip-basic", "command": "rm -rf /"},
                    {"approach": "mip-basic", "run_id": "x"}, {"approach": "mip-basic", "scenario_id": "x"}, {"approach": "mip-basic", "link_semantics": "kernel-veth"}):
            self.assertIn("unknown_field", [c for _, c in codes(raw)], raw)
        self.assertIn(("conditions.evil", "unknown_field"), codes({"approach": "mip-basic", "conditions": {"evil": 1}}))

    def test_required_and_unknown_choices(self):
        self.assertIn(("approach", "required"), codes({}))
        self.assertIn(("approach", "unknown_approach"), codes({"approach": "nope"}))
        self.assertIn(("scenario", "unknown_scenario"), codes({"approach": "mip-basic", "scenario": "nope"}))
        self.assertIn(("handoff_mode", "invalid_choice"), codes({"approach": "mip-basic", "handoff_mode": "x"}))
        self.assertEqual(codes([1]), [(None, "invalid_body")])

    def test_live_environment_is_never_exposed(self):
        self.assertEqual(codes({"approach": "mip-basic", "environment": "live"}), [("environment", "environment_not_exposed")])
        with self.assertRaises(ConfigurationError):
            S.with_(allowed_environments=("emulated", "live")).validate()

    def test_types_and_ranges(self):
        for raw, f in (({"duration_s": "10"}, "duration_s"), ({"duration_s": True}, "duration_s"), ({"duration_s": 0}, "duration_s"),
                       ({"duration_s": 1e9}, "duration_s"), ({"duration_s": float("nan")}, "duration_s"), ({"rate_pps": -1}, "rate_pps"),
                       ({"payload": 10}, "payload"), ({"payload": 64.5}, "payload"), ({"seed": -1}, "seed"), ({"seed": 2**31}, "seed"),
                       ({"detect_delay_ms": -1}, "detect_delay_ms"), ({"label": "x" * 81}, "label"), ({"label": "a\nb"}, "label")):
            self.assertIn(f, [x for x, _ in codes({"approach": "mip-basic", **raw})], raw)

    def test_probe_budget(self):
        self.assertIn(("duration_s", "probe_budget_exceeded"), codes({"approach": "mip-basic", "duration_s": 120, "rate_pps": 1000, "moves": [{"at_s": 1, "interface": "eth-b"}]}, S.with_(max_probes=1000)))

    def test_condition_ranges_use_field_names(self):
        self.assertEqual(codes({"approach": "mip-basic", "conditions": {"loss_percent": 100}}), [("conditions.loss_percent", "invalid_parameter")])
        self.assertEqual(codes({"approach": "mip-basic", "conditions": {"latency_scale": 0}}), [("conditions.latency_scale", "invalid_parameter")])
        self.assertIn(("conditions.loss_percent", "invalid_type"), codes({"approach": "mip-basic", "conditions": {"loss_percent": "2"}}))

    def test_moves_validated_against_topology(self):
        self.assertEqual(codes({"approach": "mip-basic", "duration_s": 6}), [("moves", "invalid_parameter")])                  # default 2nd move at 7 s
        self.assertEqual(codes({"approach": "mip-basic", "moves": [{"at_s": 1, "interface": "wlan9"}]}), [("moves", "invalid_parameter")])
        self.assertIn("invalid_type", [c for _, c in codes({"approach": "mip-basic", "moves": [{"at_s": 1}]})])
        self.assertIn("too_many_moves", [c for _, c in codes({"approach": "mip-basic", "moves": [{"at_s": 0.1, "interface": "eth-b"}] * 21})])

    def test_unsupported_combinations_come_from_the_runner(self):
        self.assertEqual(codes({"approach": "mip-basic", "environment": "loopback"})[0][1], "unsupported_combination")
        self.assertEqual(codes({"approach": "tcp-reconnect", "handoff_mode": "mbb"})[0][1], "unsupported_combination")
        self.assertEqual(codes({"approach": "mip-mbb", "handoff_mode": "mbb"}), [])

    def test_unavailable_approach_is_reported_not_run(self):
        try:
            import aioquic  # noqa: F401
        except ImportError:
            self.assertEqual(codes({"approach": "quic-migration"}), [("approach", "approach_unavailable")])

    def test_no_effect_condition_is_a_warning_not_an_error(self):
        issues, r = v({"approach": "tcp-reconnect", "scenario": "distant-home-agent"})
        self.assertEqual(issues, []); self.assertEqual(r.warnings[0]["code"], "condition_has_no_effect")
        self.assertEqual(v({"approach": "mip-basic", "scenario": "distant-home-agent"})[1].warnings, [])

    def test_prediction_requests(self):
        self.assertEqual(v({"approach": "mip-basic", "predict": True})[1].predict, {"model": "stage6", "algorithm": "random_forest"})
        self.assertIn(("prediction_model", "model_unavailable"), codes({"approach": "mip-basic", "predict": True, "prediction_model": "x"}))
        self.assertIn(("prediction_model", "model_unavailable"), codes({"approach": "mip-basic", "predict": True, "prediction_algorithm": "x"}))
        self.assertIn(("predict", "inconsistent"), codes({"approach": "mip-basic", "prediction_model": "stage6"}))
        s0 = S.with_(model_dirs={})
        iss, _ = validate_request({"approach": "mip-basic", "predict": True}, s0, Catalog(s0), ModelRegistry(s0))
        self.assertEqual([(i.field, i.code) for i in iss], [("prediction_model", "model_unavailable")])

    def test_fingerprint_ignores_label_but_not_parameters(self):
        a, b = v({"approach": "mip-basic", "label": "a"})[1], v({"approach": "mip-basic", "label": "b"})[1]
        self.assertEqual(a.fingerprint, b.fingerprint); self.assertNotEqual(a.request_hash, b.request_hash)
        self.assertNotEqual(a.fingerprint, v({"approach": "mip-basic", "seed": 1})[1].fingerprint)
        self.assertNotEqual(a.fingerprint, v({"approach": "mip-basic", "predict": True})[1].fingerprint)

    def test_whitelist_is_exactly_the_documented_fields(self):
        self.assertEqual(len(ALLOWED_FIELDS), 18)


class Settings(unittest.TestCase):
    def test_from_env(self):
        s = ServiceSettings.from_env({"NETSIM_API_MAX_CONCURRENT_JOBS": "3", "NETSIM_API_TOKEN": "t", "NETSIM_API_MODEL_DIRS": "a=/x,b=rel/y", "NETSIM_API_ALLOW_ROOT": "1"})
        self.assertEqual((s.max_concurrent_jobs, s.api_token, s.allow_root), (3, "t", True)); self.assertEqual(s.model_dirs["a"].as_posix(), "/x")
        self.assertTrue(s.model_dirs["b"].is_absolute())

    def test_bad_env_fails_fast(self):
        for env in ({"NETSIM_API_MAX_CONCURRENT_JOBS": "x"}, {"NETSIM_API_MAX_CONCURRENT_JOBS": "0"}, {"NETSIM_API_MODEL_DIRS": "nokey"}, {"NETSIM_API_ENVIRONMENTS": "live"}):
            with self.assertRaises(ConfigurationError, msg=env):
                ServiceSettings.from_env(env)


if __name__ == "__main__":
    unittest.main()
