"""Comparison matrix: a static capability table plus measured results from actual runs (and the explicit list of
scenarios that are unsupported or unavailable, each with its reason)."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from .approaches import APPROACHES, ENV_LABELS, SUPPORT
from .experiment import ExperimentSpec
from .runner import run_experiment


def default_specs(base: ExperimentSpec | None = None) -> list:
    """The standard comparison set (all on the same topology, probe workload, movement and detection timing)."""
    b = base or ExperimentSpec("tcp-reconnect")
    mk = lambda approach, **kw: replace(b, approach=approach, scenario_id="", **kw)
    return [
        mk("tcp-reconnect", variant="notify"),
        mk("tcp-reconnect", variant="stall-timeout", tcp_detect="timeout"),
        mk("mip-basic"),
        mk("mip-mbb"),
        mk("ro-model"),
        mk("quic-migration"),
    ]


def probe_specs(base: ExperimentSpec | None = None) -> list:
    """Scenarios that are expected NOT to run; they are executed so the refusal and its reason are on record."""
    b = base or ExperimentSpec("tcp-reconnect")
    mk = lambda approach, **kw: replace(b, approach=approach, scenario_id="", **kw)
    return [mk("tcp-reconnect", handoff_mode="mbb", variant="mbb"), mk("mip-basic", handoff_mode="mbb", variant="mbb"),
            mk("ro-model", handoff_mode="mbb", variant="mbb"), mk("quic-migration", handoff_mode="mbb", variant="mbb"),
            mk("mip-basic", environment="loopback"), mk("mip-mbb", environment="loopback"), mk("ro-model", environment="loopback"),
            mk("quic-migration", environment="loopback"), mk("ro-model", environment="live"),
            mk("tcp-reconnect", environment="live"), mk("quic-migration", environment="live")]


def capability_table() -> str:
    envs = ("emulated", "loopback", "live")
    lines = ["| Approach | Standards status | Recovery point (handoff latency ends when...) | Mobility overhead |",
             "|---|---|---|---|"]
    for a in APPROACHES.values():
        lines.append(f"| **{a.name}** (`{a.id}`) | {a.status} | {a.recovery_point} | {a.mobility_overhead} |")
    lines += ["", "| Approach | " + " | ".join(ENV_LABELS[e] for e in envs) + " |", "|---|" + "---|" * len(envs)]
    mark = {"supported": "yes", "unverified": "yes, UNVERIFIED here", "unsupported": "no"}
    for a in APPROACHES:
        cells = []
        for e in envs:
            st, why = SUPPORT[a][e]
            cells.append(f"{mark[st]}: {why}")
        lines.append(f"| `{a}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _f(x, nd=1):
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, float) else str(x))


def results_table(results: list) -> str:
    head = ("| Approach (variant) | Status | Handoff latency mean/max (ms) | Service gap mean/max (ms) | Packets lost (total) | "
            "RTT before→after, 1st handoff (ms) | Signalling per handoff (msgs / bytes) | Mobility overhead (B/pkt) | "
            "Transport connection preserved | Endpoint address preserved |")
    lines = [head, "|" + "---|" * 10]
    for r in results:
        name = f"`{r.spec.approach}`" + (f" ({r.spec.variant})" if r.spec.variant else "")
        if r.status != "ok":
            lines.append(f"| {name} | **{r.status}** | " + " | ".join(["—"] * 8) + f" |")
            continue
        m, hs = r.metrics, r.metrics["handoff_summary"]
        n = max(hs["count"], 1)
        h1 = m["handoffs"][0] if m["handoffs"] else {}
        c = m["continuity"]
        yn = lambda v: "n/a (connectionless)" if v is None else ("yes" if v else "no")
        lines.append(
            f"| {name} | ok | {_f(hs['handoff_latency_ms_mean'])} / {_f(hs['handoff_latency_ms_max'])} | "
            f"{_f(hs['service_gap_ms_mean'])} / {_f(hs['service_gap_ms_max'])} | {m['loss']['lost']} | "
            f"{_f(h1.get('rtt_before_ms'))}→{_f(h1.get('rtt_after_ms'))} | "
            f"{hs['signalling_messages_total'] / n:.1f} / {hs['signalling_bytes_total'] / n:.0f} | "
            f"{_f(m['mobility_overhead']['mobility_bytes_per_packet'])} | {yn(c.get('transport_connection_preserved'))} | "
            f"{yn(c.get('endpoint_address_preserved'))} |")
    return "\n".join(lines)


def unsupported_table(results: list) -> str:
    lines = ["| Scenario | Status | Reason |", "|---|---|---|"]
    for r in results:
        if r.status != "ok":
            s = r.spec
            sc = f"`{s.approach}` / {s.environment}" + (" / make-before-break" if s.handoff_mode == "mbb" else "")
            lines.append(f"| {sc} | {r.status} | {r.reason} |")
    return "\n".join(lines)


def write_report(results: list, out_dir: Path, base: ExperimentSpec) -> Path:
    out_dir = Path(out_dir)
    ran = [r for r in results if r.spec.environment == "emulated" and r.spec.handoff_mode == "bbm"]
    md = ["# Mobility comparison (generated)", "",
          f"Common conditions: topology `{Path(base.topology).name}`, probes {base.rate_pps:g} pps x {base.payload} B for {base.duration_s:g} s, "
          f"moves {[list(m) for m in base.moves]}, movement-detection delay {base.detect_delay_ms:g} ms, "
          f"make-before-break old-link hold {base.old_link_hold_ms:g} ms (mip-mbb only).", "",
          "## Capability matrix", "", capability_table(), "",
          "## Measured results (emulated environment, deterministic virtual time)", "", results_table(ran), "",
          "## Unsupported or unavailable scenarios (reported, not skipped)", "", unsupported_table(results), ""]
    p = out_dir / "comparison.md"
    p.write_text("\n".join(md))
    (out_dir / "comparison.json").write_text(json.dumps([
        {"approach": r.spec.approach, "environment": r.spec.environment, "variant": r.spec.variant,
         "handoff_mode": r.spec.handoff_mode, "status": r.status, "reason": r.reason,
         "metrics": r.metrics} for r in results], indent=2, sort_keys=True))
    return p


def run_matrix(out_dir, base: ExperimentSpec | None = None, include_probes: bool = True):
    base = base or ExperimentSpec("tcp-reconnect")
    specs = default_specs(base) + (probe_specs(base) if include_probes else [])
    results = [run_experiment(s, out_dir) for s in specs]
    return results, write_report(results, Path(out_dir), base)
