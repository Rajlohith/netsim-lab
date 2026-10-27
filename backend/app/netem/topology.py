"""Create / inspect / shape / measure / destroy the emulated topology."""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import plan
from .errors import CommandError, NetemError, TopologyError
from .measure import PingStats, parse_ping
from .names import is_project_ns, ns_name, validate_prefix
from .runner import Runner
from .topology_config import NetemSpec, TopologyConfig


@dataclass
class DestroyReport:
    deleted: list = field(default_factory=list)
    absent: list = field(default_factory=list)    # project names that did not exist
    refused: list = field(default_factory=list)   # names failing validation (never touched)


class TopologyManager:
    def __init__(self, cfg: TopologyConfig, runner: Runner, prefix: str,
                 state_path: Path | None = None):
        self.cfg = cfg
        self.runner = runner
        self.prefix = validate_prefix(prefix)
        self.state_path = Path(state_path) if state_path else None

    # ---------- helpers ----------
    def ns(self, node_key: str) -> str:
        if node_key not in self.cfg.nodes:
            raise TopologyError(f"unknown node {node_key!r}; known: {sorted(self.cfg.nodes)}")
        return ns_name(self.prefix, node_key)

    def _targets(self) -> list[str]:
        return [ns_name(self.prefix, k) for k in self.cfg.nodes]

    def list_namespaces(self) -> set[str]:
        res = self.runner.run(plan.ns_list(), check=False)
        names = set()
        for line in res.stdout.splitlines():
            if line.strip():
                names.add(line.split()[0])
        return names

    def _require_tools(self):
        if self.runner.dry_run or not self.runner.real_exec:
            return
        missing = [t for t in ("ip", "tc") if shutil.which(t) is None]
        if missing:
            raise TopologyError(
                f"missing tool(s): {missing}. Zorin/Ubuntu: sudo apt install iproute2 | "
                "Fedora: sudo dnf install iproute iproute-tc")

    # ---------- state file (records what *this project* created) ----------
    def _read_state(self) -> list[str]:
        if not self.state_path or not self.state_path.exists():
            return []
        try:
            data = json.loads(self.state_path.read_text())
            return [n for n in data.get("namespaces", []) if isinstance(n, str)]
        except (OSError, ValueError, AttributeError):
            return []

    def _write_state(self, names):
        if self.state_path and not self.runner.dry_run:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({
                "topology": self.cfg.name, "prefix": self.prefix, "namespaces": sorted(names),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))

    def _clear_state(self):
        if self.state_path and not self.runner.dry_run:
            self.state_path.unlink(missing_ok=True)

    # ---------- operations ----------
    def create(self) -> list[str]:
        self._require_tools()
        targets = self._targets()
        clash = sorted(set(targets) & self.list_namespaces())
        if clash:
            raise TopologyError(
                f"namespaces already exist: {clash}. Refusing to reuse them; run 'destroy' first "
                "(it only removes this project's namespaces).")
        self._write_state(targets)
        created: list[str] = []
        try:
            for cmd in plan.build_create_plan(self.cfg, self.prefix):
                self.runner.run(cmd)
                if cmd.argv[1:3] == ("netns", "add"):
                    created.append(cmd.argv[3])
        except Exception:
            for ns in reversed(created):          # roll back only what this run created
                try:
                    self.runner.run(plan.ns_del(ns), check=False)
                except NetemError:
                    pass
            self._clear_state()
            raise
        return created

    def destroy(self) -> DestroyReport:
        self._require_tools()
        report = DestroyReport()
        candidates = set(self._targets()) | set(self._read_state())
        valid = {n for n in candidates if is_project_ns(n, self.prefix)}
        report.refused = sorted(candidates - valid)
        existing = self.list_namespaces()
        for ns in sorted(valid):
            if ns in existing:
                self.runner.run(plan.ns_del(ns))   # deleting a namespace also destroys its veth ends
                report.deleted.append(ns)
            else:
                report.absent.append(ns)
        if not report.refused:
            self._clear_state()
        return report

    def status(self) -> str:
        existing = self.list_namespaces()
        out = []
        for key in self.cfg.nodes:
            ns = self.ns(key)
            if ns not in existing and not self.runner.dry_run:
                out.append(f"== {key} ({ns}): NOT PRESENT")
                continue
            out.append(f"== {key} [{self.cfg.nodes[key].role}] ({ns})")
            for cmd in plan.status_cmds(ns):
                res = self.runner.run(cmd, check=False)
                out.append(f"-- {cmd.desc}\n{res.stdout.rstrip()}")
        return "\n".join(out)

    def shape(self, link_name: str, end: str, spec: NetemSpec | None) -> list[str]:
        """Change (or clear, if spec is None/empty) netem on one or both ends of a link."""
        if end not in ("a", "b", "both"):
            raise TopologyError("end must be 'a', 'b' or 'both'")
        link = self.cfg.link(link_name)
        existing = self.list_namespaces()
        applied = []
        for which, e in link.ends():
            if end not in (which, "both"):
                continue
            ns = self.ns(e.node)
            if ns not in existing and not self.runner.dry_run:
                raise TopologyError(f"namespace {ns} does not exist; run 'create' first")
            self.runner.run(plan.shape_cmd(ns, e.ifname, spec), check=bool(spec and not spec.is_empty()))
            applied.append(f"{ns}:{e.ifname}")
        return applied

    def ping(self, src_key: str, dst_ip: str, count: int = 5, interval: float = 0.2,
             wait: int = 2) -> PingStats:
        ns = self.ns(src_key)
        if ns not in self.list_namespaces() and not self.runner.dry_run:
            raise TopologyError(f"namespace {ns} does not exist; run 'create' first")
        res = self.runner.run(plan.ping_cmd(ns, dst_ip, count, interval, wait), check=False,
                              timeout=count * interval + wait + 15)
        if self.runner.dry_run:
            return PingStats(count, 0, 100.0, None, None, None)
        if not res.stdout.strip() and res.returncode != 0:
            raise CommandError(f"ping failed: {res.stderr.strip()}", res.argv, res.returncode, res.stderr)
        return parse_ping(res.stdout)

    def set_interface(self, node_key: str, ifname: str, up: bool) -> str:
        """Bring one of a node's configured interfaces up/down (handoff simulation)."""
        ns = self.ns(node_key)
        if ifname not in {e.ifname for _, e in self.cfg.interfaces_of(node_key)}:
            raise TopologyError(f"node {node_key!r} has no configured interface {ifname!r}")
        if not self.runner.dry_run and ns not in self.list_namespaces():
            raise TopologyError(f"namespace {ns} does not exist; run 'create' first")
        self.runner.run(plan.link_set_cmd(ns, ifname, up))
        return f"{ns}:{ifname} {'up' if up else 'down'}"
