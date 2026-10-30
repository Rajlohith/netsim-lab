"""Experiment-request validation against EXPLICITLY supported parameters, scenarios and server limits.

Input: the plain dict a client sent. Output: (issues, resolved). `resolved` is None when there is any issue. Rules, in order:

  1. only whitelisted fields are accepted (an unknown field is an error, never ignored); client-controlled paths, commands, topology
     files, run ids and scenario ids do not exist as parameters at all;
  2. every value has the right type and lies within the API limits (ServiceSettings, tighter than the runner's own limits);
  3. the scenario preset and explicit fields are merged into an ExperimentSpec (explicit fields win);
  4. the EXISTING runner `check()` decides whether the approach/environment/handoff-mode combination is supported and available
     here and whether the parameters make sense for the topology (unsupported / unavailable / invalid);
  5. prediction requests need a usable model bundle.
Issues are {field, code, message}; warnings (non-fatal, e.g. a Home-Agent-only setting on an approach without a Home Agent) likewise."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field

from ..compare.experiment import ENVIRONMENTS, HANDOFF_MODES, ExperimentSpec
from ..compare.runner import check, condition_applicability
from ..netem.conditions import NetworkConditions
from .catalog import Catalog
from .model_registry import ModelRegistry
from .settings import ServiceSettings

ALLOWED_FIELDS = ("approach", "scenario", "environment", "handoff_mode", "duration_s", "rate_pps", "payload", "seed", "initial_iface", "moves",
                  "tcp_detect", "old_link_hold_ms", "detect_delay_ms", "conditions", "predict", "prediction_model", "prediction_algorithm", "label")
CONDITION_FIELDS = ("latency_scale", "loss_percent", "ha_distance_ms", "registration_loss_percent", "ha_processing_ms")
_IFACE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
DEFAULT_SCENARIO = "baseline"
DEFAULT_ALGORITHM = "random_forest"
SPEC_FIELD_NAMES = {"duration_s", "rate_pps", "payload", "detect_delay_ms", "probe_start_s", "old_link_hold_ms", "tcp_detect",
                    "initial_iface", "link_semantics", "latency_scale", "loss_percent", "ha_distance_ms", "registration_loss_percent", "ha_processing_ms"}


@dataclass(frozen=True)
class Issue:
    field: str | None
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass
class Resolved:
    spec: ExperimentSpec
    request: dict                 # the EFFECTIVE request (every field filled in) - stored with the experiment
    approach: str
    scenario: str
    environment: str
    label: str | None
    predict: dict | None          # {"model": id, "algorithm": name} or None
    fingerprint: str              # identical configuration => identical fingerprint (duplicate-run prevention)
    request_hash: str             # idempotency-key replay check
    warnings: list = field(default_factory=list)
    support: dict = field(default_factory=dict)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _field_of(msg: str) -> str | None:
    first = msg.split(" ", 1)[0].rstrip(":")
    if first in SPEC_FIELD_NAMES:
        return f"conditions.{first}" if first in CONDITION_FIELDS else first
    if msg.startswith("move "):
        return "moves"
    if msg.startswith("cannot use topology"):
        return None
    return None


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def validate_request(raw, settings: ServiceSettings, catalog: Catalog, models: ModelRegistry):
    issues: list[Issue] = []
    add = lambda f, c, m: issues.append(Issue(f, c, m))
    if not isinstance(raw, dict):
        return [Issue(None, "invalid_body", "the request body must be a JSON object")], None
    for k in sorted(set(raw) - set(ALLOWED_FIELDS), key=str):
        add(str(k), "unknown_field", f"{k!r} is not a supported parameter; supported: {list(ALLOWED_FIELDS)}")

    base = ExperimentSpec("mip-basic")                     # the defaults of the common experiment interface

    # ---- approach / scenario / environment / mode ----------------------------------------------------------------------
    approach = raw.get("approach")
    if approach is None:
        add("approach", "required", "approach is required; see GET /api/approaches")
    elif not isinstance(approach, str) or approach not in catalog.approach_ids():
        add("approach", "unknown_approach", f"unknown approach {approach!r}; supported: {catalog.approach_ids()}")
    scenario = raw.get("scenario", DEFAULT_SCENARIO)
    if not isinstance(scenario, str) or catalog.scenario(scenario) is None:
        add("scenario", "unknown_scenario", f"unknown scenario {scenario!r}; available: {catalog.scenario_ids()}")
    environment = raw.get("environment", "emulated")
    if not isinstance(environment, str) or environment not in ENVIRONMENTS:
        add("environment", "unknown_environment", f"unknown environment {environment!r}; known: {list(ENVIRONMENTS)}")
    elif environment not in settings.allowed_environments:
        why = ("privileged network-namespace operations are never exposed through the API; use the CLI (docs/PRIVILEGED_OPERATIONS.md)"
               if environment == "live" else "this environment is disabled by the server configuration")
        add("environment", "environment_not_exposed", f"environment {environment!r} cannot be used through the API: {why}; exposed: {list(settings.allowed_environments)}")
    handoff_mode = raw.get("handoff_mode", "bbm")
    if not isinstance(handoff_mode, str) or handoff_mode not in HANDOFF_MODES:
        add("handoff_mode", "invalid_choice", f"handoff_mode must be one of {list(HANDOFF_MODES)}, got {handoff_mode!r}")
    tcp_detect = raw.get("tcp_detect", base.tcp_detect)
    if tcp_detect not in ("event", "timeout"):
        add("tcp_detect", "invalid_choice", f"tcp_detect must be 'event' or 'timeout', got {tcp_detect!r}")

    preset = (catalog.scenario(scenario) or {}).get("parameters", {}) if isinstance(scenario, str) else {}

    # ---- numbers ---------------------------------------------------------------------------------------------------------
    def number(name, default, lo, hi, *, integer=False, lo_inclusive=False):
        v = raw.get(name, default)
        ok = _is_int(v) if integer else _is_num(v)
        if not ok:
            add(name, "invalid_type", f"{name} must be {'an integer' if integer else 'a number'}, got {v!r}")
            return default
        if not ((lo <= v if lo_inclusive else lo < v) and v <= hi):
            add(name, "out_of_range", f"{name} must be in {'[' if lo_inclusive else '('}{lo}, {hi}], got {v!r}")
        return v

    duration = number("duration_s", preset.get("duration_s", base.duration_s), 0, settings.max_duration_s)
    rate = number("rate_pps", base.rate_pps, 0, settings.max_rate_pps)
    payload = number("payload", base.payload, 36, 1472, integer=True, lo_inclusive=True)
    seed = number("seed", 0, 0, 2**31 - 1, integer=True, lo_inclusive=True)
    hold = number("old_link_hold_ms", base.old_link_hold_ms, 0, 10_000, lo_inclusive=True)
    detect = number("detect_delay_ms", preset.get("detect_delay_ms", base.detect_delay_ms), 0, 10_000, lo_inclusive=True)
    if _is_num(duration) and _is_num(rate) and duration * rate > settings.max_probes:
        add("duration_s", "probe_budget_exceeded", f"duration_s * rate_pps = {duration * rate:g} probes exceeds the server limit of {settings.max_probes}")

    iface = raw.get("initial_iface", base.initial_iface)
    if not isinstance(iface, str) or not _IFACE.match(iface):
        add("initial_iface", "invalid_type", f"initial_iface must be an interface name ([A-Za-z0-9_.-], 1-32 chars), got {iface!r}")

    # ---- moves -------------------------------------------------------------------------------------------------------------
    moves = base.moves
    if "moves" in raw:
        m = raw["moves"]
        if not isinstance(m, list) or not m:
            add("moves", "invalid_type", "moves must be a non-empty list of {at_s, interface} objects")
        elif len(m) > settings.max_moves:
            add("moves", "too_many_moves", f"at most {settings.max_moves} moves are allowed, got {len(m)}")
        else:
            parsed, bad = [], False
            for i, x in enumerate(m):
                if not (isinstance(x, dict) and set(x) == {"at_s", "interface"} and _is_num(x["at_s"]) and isinstance(x["interface"], str)
                        and _IFACE.match(x["interface"])):
                    add(f"moves[{i}]", "invalid_type", "each move must be exactly {\"at_s\": <number>, \"interface\": <name>}")
                    bad = True
                else:
                    parsed.append((float(x["at_s"]), x["interface"]))
            if not bad:
                moves = tuple(parsed)
    elif "moves" in preset:
        moves = tuple((float(t), str(i)) for t, i in preset["moves"])

    # ---- conditions --------------------------------------------------------------------------------------------------------
    cond = dict(preset.get("conditions") or {})
    if "conditions" in raw:
        c = raw["conditions"]
        if not isinstance(c, dict):
            add("conditions", "invalid_type", "conditions must be an object")
        else:
            for k in sorted(set(c) - set(CONDITION_FIELDS), key=str):
                add(f"conditions.{k}", "unknown_field", f"{k!r} is not a supported condition; supported: {list(CONDITION_FIELDS)}")
            for k in CONDITION_FIELDS:
                if k in c:
                    if not _is_num(c[k]):
                        add(f"conditions.{k}", "invalid_type", f"{k} must be a number, got {c[k]!r}")
                    else:
                        cond[k] = c[k]

    # ---- prediction / label ------------------------------------------------------------------------------------------------
    predict_on = raw.get("predict", False)
    if not isinstance(predict_on, bool):
        add("predict", "invalid_type", f"predict must be true or false, got {predict_on!r}")
        predict_on = False
    model_id, algorithm = raw.get("prediction_model"), raw.get("prediction_algorithm")
    if not predict_on and (model_id is not None or algorithm is not None):
        add("predict", "inconsistent", "prediction_model/prediction_algorithm were given but predict is not true")
    predict = None
    if predict_on:
        ids = models.ids()
        model_id = model_id if model_id is not None else (ids[0] if ids else None)
        algorithm = algorithm if algorithm is not None else DEFAULT_ALGORITHM
        if model_id is None:
            add("prediction_model", "model_unavailable", "no prediction model is configured on this server (NETSIM_API_MODEL_DIRS)")
        elif not isinstance(model_id, str) or not isinstance(algorithm, str):
            add("prediction_model", "invalid_type", "prediction_model and prediction_algorithm must be strings")
        else:
            ok, why = models.require_usable(model_id, algorithm)
            if not ok:
                add("prediction_model", "model_unavailable", why)
            else:
                predict = {"model": model_id, "algorithm": algorithm}
    label = raw.get("label")
    if label is not None:
        if not isinstance(label, str) or len(label.strip()) > 80 or not label.strip().isprintable():
            add("label", "invalid_type", "label must be printable text of at most 80 characters")
            label = None
        else:
            label = label.strip() or None

    if issues:
        return issues, None

    # ---- build the spec, then let the EXISTING runner decide support/availability/sanity -----------------------------------
    conditions = NetworkConditions(seed=seed, **cond)
    cond_bad = conditions.validate(None)
    if cond_bad:
        return [Issue(_field_of(cond_bad), "invalid_parameter", cond_bad)], None
    spec = ExperimentSpec(approach, environment, scenario_id=f"api-{approach}-{environment}-{scenario}", run_id="run-1", duration_s=float(duration),
                          rate_pps=float(rate), payload=int(payload), initial_iface=iface, moves=moves, handoff_mode=handoff_mode,
                          old_link_hold_ms=float(hold), detect_delay_ms=float(detect), tcp_detect=tcp_detect, seed=int(seed), conditions=conditions)
    status, reason = check(spec)
    if status != "ok":
        code = {"unsupported": "unsupported_combination", "unavailable": "approach_unavailable", "invalid": "invalid_parameter"}[status]
        text = reason.removeprefix("invalid experiment parameters: ")
        return [Issue(_field_of(text) if status == "invalid" else ("approach" if status == "unavailable" else None), code, reason)], None

    warnings = [{"field": f"conditions.{k}" if k in CONDITION_FIELDS else k, "code": "condition_has_no_effect", "message": v}
                for k, v in condition_applicability(spec).items() if v != "applies"]
    if predict and environment != "emulated":
        warnings.append({"field": "predict", "code": "prediction_out_of_distribution",
                         "message": f"the model was trained on emulator data; this run's environment is {environment!r}, so its predictions are outside the training distribution"})
    eff = {"approach": approach, "scenario": scenario, "environment": environment, "handoff_mode": handoff_mode, "duration_s": float(duration),
           "rate_pps": float(rate), "payload": int(payload), "seed": int(seed), "initial_iface": iface,
           "moves": [{"at_s": t, "interface": i} for t, i in moves], "tcp_detect": tcp_detect, "old_link_hold_ms": float(hold),
           "detect_delay_ms": float(detect), "conditions": {k: getattr(conditions, k) for k in CONDITION_FIELDS},
           "predict": bool(predict), "prediction_model": predict["model"] if predict else None,
           "prediction_algorithm": predict["algorithm"] if predict else None, "label": label}
    fp = hashlib.sha256(_canon({"spec": spec.to_json(), "predict": predict}).encode()).hexdigest()
    rh = hashlib.sha256(_canon(eff).encode()).hexdigest()
    return [], Resolved(spec, eff, approach, scenario, environment, label, predict, fp, rh, warnings, {"status": status, "reason": reason or None})
