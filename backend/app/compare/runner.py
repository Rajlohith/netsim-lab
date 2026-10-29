"""The common experiment interface: run_experiment(spec, out_dir) for ANY approach.

Always leaves an `experiment.json` in the run directory saying what was asked, whether it ran, and why not if it did
not (status: ok | unsupported | unavailable | failed). Nothing is silently skipped."""
from __future__ import annotations

import json
import platform
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

from ..mobileip.analyze import analyze_mip_run
from ..mobileip.events import read_events as read_mip_events
from ..mobileip.mobility_sim import MobilitySim, MobilitySpec
from ..mobileip.ro_sim import RoSim
from ..netem.conditions import NetworkConditions, apply_conditions
from ..traffic.clock import iso_utc, wall_ns
from . import events as ev
from .approaches import APPROACHES, ENV_LABELS, MBB_CAPABLE, MBB_REASON, SUPPORT
from .experiment import ENVIRONMENTS, HANDOFF_MODES, ExperimentSpec
from .normalize import normalize
from .quic_sim import QuicLab, quic_available


@dataclass
class ExperimentResult:
    status: str                      # ok | unsupported | unavailable | invalid | failed
    reason: str
    spec: ExperimentSpec
    run_dir: Path
    metrics: dict | None = None


TUNNELLED = ("mip-basic", "mip-mbb", "ro-model")
MAX_TUNNEL_PAYLOAD = 1500 - 20 - 8 - 20          # link MTU - IPv4 - UDP - IP-in-IP header


def validate(spec: ExperimentSpec):
    """Parameter sanity checks. Returns a reason string, or None if the parameters make sense."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    for name, lo, hi in (("duration_s", 0, 3600), ("rate_pps", 0, 5000)):
        v = getattr(spec, name)
        if not num(v) or not (lo < v <= hi):
            return f"{name} must be a number in ({lo}, {hi}], got {v!r}"
    if not isinstance(spec.payload, int) or isinstance(spec.payload, bool) or not (36 <= spec.payload <= 1472):
        return f"payload must be an integer in [36, 1472] bytes (36-byte probe header), got {spec.payload!r}"
    for name in ("detect_delay_ms", "probe_start_s", "old_link_hold_ms", "tcp_detect_timeout_s"):
        v = getattr(spec, name)
        if not num(v) or v < 0 or (name == "tcp_detect_timeout_s" and v == 0):
            return f"{name} must be a non-negative number (timeout > 0), got {v!r}"
    if spec.probe_start_s >= spec.duration_s:
        return "probe_start_s must be smaller than duration_s"
    if spec.tcp_detect not in ("event", "timeout"):
        return f"tcp_detect must be 'event' or 'timeout', got {spec.tcp_detect!r}"
    if spec.link_semantics not in ("both-ends", "kernel-veth"):
        return f"link_semantics must be 'both-ends' or 'kernel-veth', got {spec.link_semantics!r}"
    try:
        from ..netem.topology_config import load_config
        cfg = load_config(spec.topology)
        mn = next(k for k, n in cfg.nodes.items() if n.role == "mobile-node")
        ifaces = {e.ifname for _l, e in cfg.interfaces_of(mn)}
    except Exception as e:
        return f"cannot use topology {spec.topology}: {e}"
    if not isinstance(spec.conditions, NetworkConditions):
        return f"conditions must be a NetworkConditions, got {type(spec.conditions).__name__}"
    cond_bad = spec.conditions.validate(cfg)
    if cond_bad:
        return cond_bad
    if spec.initial_iface not in ifaces:
        return f"initial_iface {spec.initial_iface!r} is not a mobile-node interface; known: {sorted(ifaces)}"
    cur, last_t = spec.initial_iface, -1.0
    for i, mv in enumerate(spec.moves, 1):
        if not (isinstance(mv, (tuple, list)) and len(mv) == 2 and num(mv[0])):
            return f"move {i} must be (seconds, interface), got {mv!r}"
        t, ifn = mv
        if not (0 <= t < spec.duration_s):
            return f"move {i} at {t}s is outside the run (0 <= t < duration_s={spec.duration_s})"
        if t <= last_t:
            return f"move {i} at {t}s is not later than the previous move ({last_t}s): moves must be in increasing time order"
        if ifn not in ifaces:
            return f"move {i} goes to unknown interface {ifn!r}; known: {sorted(ifaces)}"
        if ifn == cur:
            return f"move {i} goes to {ifn!r}, the interface the mobile node is already on"
        cur, last_t = ifn, t
    return None


def check(spec: ExperimentSpec):
    """Returns (status, reason) BEFORE anything runs: ('ok','') or ('unsupported'|'unavailable'|'invalid', reason)."""
    if spec.approach not in APPROACHES:
        return "unsupported", f"unknown approach {spec.approach!r}; known: {sorted(APPROACHES)}"
    if spec.environment not in ENVIRONMENTS:
        return "unsupported", f"unknown environment {spec.environment!r}; known: {list(ENVIRONMENTS)}"
    if spec.handoff_mode not in HANDOFF_MODES:
        return "unsupported", f"unknown handoff_mode {spec.handoff_mode!r}; known: {list(HANDOFF_MODES)}"
    bad = validate(spec)
    if bad:
        return "invalid", f"invalid experiment parameters: {bad}"
    st, why = SUPPORT[spec.approach][spec.environment]
    if st == "unsupported":
        return "unsupported", f"{spec.approach} in the {ENV_LABELS[spec.environment]} environment: {why}"
    if spec.handoff_mode == "mbb" and spec.approach not in MBB_CAPABLE:
        return "unsupported", f"{spec.approach} with make-before-break: {MBB_REASON[spec.approach]}"
    if spec.approach in TUNNELLED and spec.payload > MAX_TUNNEL_PAYLOAD:
        return "unsupported", (f"{spec.approach} with a {spec.payload}-byte payload: payload + 28 B UDP/IPv4 + 20 B tunnel header exceeds the "
                               f"1500-byte link MTU (max {MAX_TUNNEL_PAYLOAD}); fragmentation and path-MTU discovery are not implemented, "
                               "so every packet would be dropped")
    if spec.approach == "quic-migration" and not quic_available():
        return "unavailable", "the aioquic library is not installed (pip install aioquic); the QUIC adapter cannot run"
    return "ok", ""


def run_experiment(spec: ExperimentSpec, out_dir, live=None) -> ExperimentResult:
    out_dir = Path(out_dir)
    run_dir = out_dir / spec.scenario_id / spec.run_id
    status, reason = check(spec)
    metrics = None
    if status == "ok":
        try:
            _dispatch(spec, out_dir, live)
            metrics = normalize(run_dir)
        except Exception as e:                                  # reported, never swallowed
            status, reason = "failed", f"{type(e).__name__}: {e}"
            (run_dir.parent).mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "traceback.txt").write_text(traceback.format_exc())
    run_dir.mkdir(parents=True, exist_ok=True)
    info = APPROACHES.get(spec.approach)
    (run_dir / "experiment.json").write_text(json.dumps({
        "schema_version": 1, "status": status, "reason": reason, "spec": spec.to_json(),
        "effective_handoff_mode": "mbb" if spec.approach in MBB_CAPABLE else spec.handoff_mode,
        "approach": None if info is None else {"name": info.name, "status": info.status, "mechanism": info.mechanism,
                                               "recovery_point": info.recovery_point, "scope_limits": info.scope_limits},
        "support": SUPPORT.get(spec.approach, {}).get(spec.environment),
        "generated_wall_iso": iso_utc(wall_ns()),
        "software": software_versions(),
        "effective_links": effective_links(spec),
        "condition_applicability": condition_applicability(spec),
    }, indent=2, sort_keys=True))
    return ExperimentResult(status, reason, spec, run_dir, metrics)


_SOFTWARE_CACHE: dict = {}


def software_versions() -> dict:
    """Versions of the interpreter, platform and the libraries that influence results (Stage 7: recorded with every run).
    The first three keys are the ones Stage 5/6 wrote; the rest are additions. Cached per process."""
    if not _SOFTWARE_CACHE:
        import importlib
        from .. import __version__ as project_version
        d = {"python": platform.python_version(), "platform": sys.platform, "aioquic_available": quic_available(),
             "os": platform.platform(), "netsim_lab": project_version, "framework": "stage7-experiments"}
        for mod in ("numpy", "scipy", "pandas", "matplotlib", "sklearn", "joblib", "yaml"):
            try:
                d[mod] = getattr(importlib.import_module(mod), "__version__", "unknown")
            except Exception:                                   # not installed: say so instead of omitting silently
                d[mod] = None
        _SOFTWARE_CACHE.update(d)
    return dict(_SOFTWARE_CACHE)


def effective_links(spec: ExperimentSpec):
    """The link parameters the run actually used (topology file + conditions): one-way delay and per-direction loss.
    Only the parameters the emulator honours are listed (jitter, rate and queue limits in the YAML are ignored by it)."""
    try:
        from ..netem.topology_config import load_config
        cfg = apply_conditions(load_config(spec.topology), spec.conditions)
        return [{"link": l.name, "delay_ms": l.effective_netem(l.a).delay_ms,
                 "loss_percent_a_to_b": l.effective_netem(l.a).loss_percent,
                 "loss_percent_b_to_a": l.effective_netem(l.b).loss_percent} for l in cfg.links]
    except Exception as e:                                       # an invalid spec has no effective topology
        return {"unavailable": f"{type(e).__name__}: {e}"}


def condition_applicability(spec: ExperimentSpec) -> dict:
    """For each NON-default condition: does it apply to this approach? (a Home Agent distance cannot affect an approach that
    never uses the Home Agent; the run is still executed, and the note says the setting had no path to act through)."""
    c, ha_users = spec.conditions, TUNNELLED
    uses_ha = spec.approach in ha_users
    out = {}
    for name, active in (("ha_distance_ms", c.ha_distance_ms > 0), ("registration_loss_percent", c.registration_loss_percent > 0),
                         ("ha_processing_ms", c.ha_processing_ms != 1.0)):
        if active:
            out[name] = ("applies" if uses_ha else
                         f"not applicable: {spec.approach} has no Home Agent / Mobile IP registration, the setting has no effect")
    return out


def _dispatch(spec, out_dir, live):
    env, a = spec.environment, spec.approach
    if env == "emulated":
        if a in ("mip-basic", "mip-mbb", "ro-model"):
            return _run_mip_emulated(spec, out_dir)
        if a == "tcp-reconnect":
            from .transport_lab import TcpLab
            return TcpLab(spec, out_dir).run()
        if a == "quic-migration":
            return QuicLab(spec, out_dir).run()
    if env == "loopback" and a == "tcp-reconnect":
        from .loopback import run_loopback_tcp
        return run_loopback_tcp(spec, out_dir)
    if env == "live" and a in ("mip-basic", "mip-mbb"):
        return _run_mip_live(spec, out_dir, live)
    raise NotImplementedError(f"{a}/{env}")


def _mip_summary(spec, run_dir, mm, ro_stats=None, mode="emulated-mip"):
    if ro_stats and ro_stats["packets"]:
        ov = {"mobility_bytes_per_packet": (ro_stats["outer_bytes"] - ro_stats["inner_bytes"]) / ro_stats["packets"],
              "packets": ro_stats["packets"], "basis": "counted from real IP-in-IP packets sent directly by the correspondent"}
    else:
        t = mm["tunnel"]
        ov = {"mobility_bytes_per_packet": t["overhead_bytes_per_packet"], "packets": t.get("packets"),
              "basis": t["basis"] if t["measured"] else "analytic (RFC 2003 header; kernel tunnel bytes not observed)"}
    return {"approach": spec.approach, "environment": spec.environment, "mode": mode, "overhead": ov,
            "continuity": {"transport_connection_preserved": None, "endpoint_address_preserved": True,
                           "note": "UDP probes are connectionless; the home address stays valid for the peer"},
            "model": {"protocol": ("educational Mobile IPv4-style protocol (app.mobileip), NOT RFC 5944 compliant" if spec.approach != "ro-model"
                                   else "educational Mobile IP + simplified route-optimisation model (app/mobileip/ro.py), NOT Mobile IPv6"),
                      "handoff_mode": "mbb" if spec.approach == "mip-mbb" else "bbm",
                      "old_link_hold_ms": spec.old_link_hold_ms if spec.approach == "mip-mbb" else None}}


def _run_mip_emulated(spec, out_dir):
    kind = {"mip-basic": "basic", "mip-mbb": "mbb", "ro-model": "ro"}[spec.approach]
    ms = MobilitySpec(scenario_id=spec.scenario_id, run_id=spec.run_id, duration_s=spec.duration_s, rate_pps=spec.rate_pps,
                      probe_payload=spec.payload, initial_iface=spec.initial_iface, moves=tuple(spec.moves),
                      lifetime_s=spec.lifetime_s, detect_delay_ms=spec.detect_delay_ms, probe_start_s=spec.probe_start_s,
                      make_before_break=(kind == "mbb"), old_link_hold_ms=spec.old_link_hold_ms, topology=spec.topology,
                      link_semantics=spec.link_semantics, conditions=spec.conditions, ha_processing_ms=spec.conditions.ha_processing_ms)
    sim = RoSim(ms, out_dir, ro_lifetime_s=spec.ro_lifetime_s) if kind == "ro" else MobilitySim(ms, out_dir)
    run_dir = sim.run()
    mm = analyze_mip_run(run_dir)
    rows, _ = read_mip_events(run_dir)
    ev.write_rows(run_dir / "approach_events.csv", ev.from_mip_events(rows, spec.approach, "ro" if kind == "ro" else "registration"))
    (run_dir / "experiment_summary.json").write_text(json.dumps(
        _mip_summary(spec, run_dir, mm, getattr(sim, "ro_stats", None) if kind == "ro" else None), indent=2, sort_keys=True))
    return run_dir


def _run_mip_live(spec, out_dir, live):
    if live is None:
        raise RuntimeError("live environment needs a LiveContext(mgr=TopologyManager) with an existing lab and sudo")
    from ..mobileip.demo import DemoSpec, run_demo
    dspec = DemoSpec(scenario_id=spec.scenario_id, duration_s=spec.duration_s, rate_pps=spec.rate_pps, size=spec.payload,
                     lifetime_s=spec.lifetime_s, initial_iface=spec.initial_iface, moves=tuple(spec.moves),
                     make_before_break=(spec.approach == "mip-mbb"), old_link_hold_ms=spec.old_link_hold_ms)
    run_dir, mm = run_demo(dspec, live.mgr, Path(out_dir), run_id=spec.run_id)
    rows, _ = read_mip_events(run_dir)
    ev.write_rows(run_dir / "approach_events.csv", ev.from_mip_events(rows, spec.approach))
    (run_dir / "experiment_summary.json").write_text(json.dumps(_mip_summary(spec, run_dir, mm, mode="live-mip"), indent=2, sort_keys=True))
    return run_dir


@dataclass
class LiveContext:
    mgr: object
