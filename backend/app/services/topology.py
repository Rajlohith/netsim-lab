"""Read-only view of the lab topology for the dashboard (GET /api/topology).

Nothing is invented here: the document is the Stage 2 loader's parse (`app.netem.topology_config.load_config`) of the same file the
experiments use (`app.compare.experiment.DEFAULT_TOPOLOGY`). Link impairments are the *configured* `tc netem` values; whether a run
applies them depends on its environment (see `notes`)."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..compare.experiment import DEFAULT_TOPOLOGY
from ..netem.errors import ConfigError
from ..netem.topology_config import load_config
from .errors import ConfigurationError
from .settings import ROOT


def _netem(spec) -> dict:
    return {k: v for k, v in asdict(spec).items()}


def topology_view(path: Path | None = None) -> dict:
    path = Path(path or DEFAULT_TOPOLOGY)
    try:
        cfg = load_config(path)
    except (ConfigError, OSError) as e:
        raise ConfigurationError(f"cannot read the topology {path}: {e}") from e
    try:
        shown = str(path.resolve().relative_to(ROOT))
    except ValueError:
        shown = path.name
    links = []
    for l in cfg.links:
        ends = {}
        for side, end in l.ends():
            ends[side] = {"node": end.node, "ifname": end.ifname, "address": str(end.address), "netem": _netem(l.effective_netem(end))}
        links.append({"name": l.name, "a": ends["a"], "b": ends["b"]})
    nodes = []
    for key, n in cfg.nodes.items():
        nodes.append({"key": key, "role": n.role, "forwarding": n.forwarding,
                      "interfaces": [{"ifname": e.ifname, "address": str(e.address), "link": l.name} for l, e in cfg.interfaces_of(key)]})
    routes = [{"node": r.node, "to": r.to, "via": str(r.via), "metric": r.metric} for r in cfg.routes]
    return {"name": cfg.name, "source_file": shown, "nodes": nodes, "links": links, "routes": routes,
            "notes": ["This is the configured lab topology (the file the experiments use). Link delay/jitter/rate/loss are the configured `tc netem` values.",
                      "Emulated runs build their links from this file in the discrete-event simulator (virtual time); loopback runs use real loopback "
                      "sockets with no path delay; privileged live namespace runs (CLI only, never through the API) apply the values with tc netem."]}
