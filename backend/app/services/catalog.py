"""What the API can run: mobility approaches (from the Stage 5 registry), network scenarios (config/api/scenarios.yaml) and limits.

Nothing here is a second source of truth: approaches, their documentation strings and where they can run come from
app.compare.approaches; availability is decided by the runner's own `check()`."""
from __future__ import annotations

from pathlib import Path

import yaml

from ..compare.approaches import APPROACHES, MBB_CAPABLE, MBB_REASON, SUPPORT, VARIANTS
from ..compare.experiment import ENVIRONMENTS, ExperimentSpec
from ..compare.quic_sim import quic_available
from ..compare.runner import TUNNELLED, check
from .errors import ConfigurationError
from .settings import ServiceSettings
from .topology import topology_view

_PARAM_KEYS = {"duration_s", "detect_delay_ms", "moves", "conditions"}
_COND_KEYS = {"latency_scale", "loss_percent", "ha_distance_ms", "registration_loss_percent", "ha_processing_ms"}
_SCEN_KEYS = {"id", "title", "description", "parameters", "requires_home_agent"}


def load_scenarios(path: Path) -> list[dict]:
    try:
        doc = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ConfigurationError(f"cannot read scenarios file {path}: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("scenarios"), list) or not doc["scenarios"]:
        raise ConfigurationError(f"{path}: expected a mapping with a non-empty 'scenarios' list")
    out, seen = [], set()
    for i, s in enumerate(doc["scenarios"]):
        where = f"{path}: scenarios[{i}]"
        if not isinstance(s, dict) or not isinstance(s.get("id"), str):
            raise ConfigurationError(f"{where}: needs a string 'id'")
        extra = set(s) - _SCEN_KEYS
        if extra:
            raise ConfigurationError(f"{where} ({s['id']}): unknown key(s) {sorted(extra)}")
        if s["id"] in seen:
            raise ConfigurationError(f"{where}: duplicate scenario id {s['id']!r}")
        seen.add(s["id"])
        p = s.get("parameters") or {}
        if not isinstance(p, dict) or set(p) - _PARAM_KEYS:
            raise ConfigurationError(f"{where} ({s['id']}): parameters may only contain {sorted(_PARAM_KEYS)}")
        if set(p.get("conditions") or {}) - _COND_KEYS:
            raise ConfigurationError(f"{where} ({s['id']}): conditions may only contain {sorted(_COND_KEYS)}")
        out.append({"id": s["id"], "title": str(s.get("title", s["id"])), "description": str(s.get("description", "")),
                    "requires_home_agent": bool(s.get("requires_home_agent", False)), "parameters": p})
    return out


class Catalog:
    def __init__(self, settings: ServiceSettings):
        self.settings = settings
        self._scenarios = load_scenarios(settings.scenarios_file)
        self._by_id = {s["id"]: s for s in self._scenarios}

    # ---- scenarios ------------------------------------------------------------------------------------------------------
    def scenario_ids(self) -> list[str]:
        return [s["id"] for s in self._scenarios]

    def scenario(self, sid: str) -> dict | None:
        return self._by_id.get(sid)

    def scenarios(self) -> list[dict]:
        base = ExperimentSpec("mip-basic")
        out = []
        for s in self._scenarios:
            p = s["parameters"]
            moves = p.get("moves", [list(m) for m in base.moves])
            out.append({
                "id": s["id"], "title": s["title"], "description": s["description"],
                "parameters": {"duration_s": p.get("duration_s", base.duration_s), "detect_delay_ms": p.get("detect_delay_ms", base.detect_delay_ms),
                               "moves": [{"at_s": float(t), "interface": i} for t, i in moves],
                               "conditions": {"latency_scale": 1.0, "loss_percent": 0.0, "ha_distance_ms": 0.0,
                                              "registration_loss_percent": 0.0, "ha_processing_ms": 1.0, **(p.get("conditions") or {})}},
                "defaults_from": "ExperimentSpec defaults (backend/app/compare/experiment.py) for every parameter not listed in the scenario file",
                "requires_home_agent": s["requires_home_agent"],
                "effective_for_approaches": sorted(TUNNELLED) if s["requires_home_agent"] else sorted(APPROACHES),
                "note": ("the setting acts through the Home Agent: for the other approaches the run executes unchanged and the response carries a warning"
                         if s["requires_home_agent"] else None),
                "source": "config/api/scenarios.yaml (one-factor levels of the Stage 7 example campaign)",
            })
        return out

    # ---- topology -------------------------------------------------------------------------------------------------------
    def topology(self) -> dict:
        return topology_view()

    # ---- approaches -----------------------------------------------------------------------------------------------------
    def approaches(self) -> list[dict]:
        out = []
        for aid, info in APPROACHES.items():
            envs = {}
            for env in ENVIRONMENTS:
                st, why = SUPPORT[aid][env]
                exposed = env in self.settings.allowed_environments
                if exposed:
                    chk, chk_why = check(ExperimentSpec(aid, env))
                    avail = {"runnable_here": chk == "ok", "check": chk, "check_reason": chk_why or None}
                else:
                    avail = {"runnable_here": False, "check": "not_exposed",
                             "check_reason": ("privileged network-namespace operations are never exposed through the API; use the CLI (docs/PRIVILEGED_OPERATIONS.md)"
                                              if env == "live" else "environment disabled by the server configuration")}
                envs[env] = {"support": st, "support_reason": why, "exposed_via_api": exposed, **avail}
            out.append({
                "id": aid, "name": info.name, "standards_status": info.status, "mechanism": info.mechanism,
                "recovery_point": info.recovery_point, "mobility_overhead": info.mobility_overhead, "scope_limits": info.scope_limits,
                "uses_home_agent": aid in TUNNELLED,
                "supports_make_before_break": aid in MBB_CAPABLE,
                "make_before_break_note": None if aid in MBB_CAPABLE else MBB_REASON.get(aid),
                "variants": [{"label": label, "overrides": kw} for label, kw in VARIANTS.get(aid, ())],
                "environments": envs,
            })
        return out

    def approach_ids(self) -> list[str]:
        return list(APPROACHES)

    # ---- everything a client needs to build a valid request -------------------------------------------------------------
    def capabilities(self, *, model_ids: list[str]) -> dict:
        s = self.settings
        return {
            "environments": {"requested_allowed": list(s.allowed_environments), "never_exposed": ["live"],
                             "why_live_is_not_exposed": "it needs sudo/CAP_NET_ADMIN to create network namespaces; the API never runs privileged operations"},
            "limits": {"max_duration_s": s.max_duration_s, "max_rate_pps": s.max_rate_pps, "max_probes": s.max_probes, "max_moves": s.max_moves,
                       "payload_bytes": [36, 1472], "seed": [0, 2**31 - 1], "label_max_chars": 80,
                       "max_concurrent_jobs": s.max_concurrent_jobs, "max_queue_length": s.max_queue_length, "max_runtime_s": s.max_runtime_s},
            "aioquic_installed": quic_available(),
            "scenarios": self.scenario_ids(), "approaches": self.approach_ids(), "prediction_models": model_ids,
            "live_updates": {"websocket": False, "polling": True,
                             "reason": ("experiments run in the simulator's virtual time and produce their metrics only after the run has finished; there is no "
                                        "metric stream to forward, so a WebSocket would have nothing true to send. Poll GET /api/experiments/{id} and "
                                        "GET /api/experiments/{id}/transitions?after_seq=N."),
                             "live_metrics_available": False},
            "data_origin": "All results are simulator-generated (virtual time) or real kernel TCP over loopback; none is a measurement of a real network.",
        }
