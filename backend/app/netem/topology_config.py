"""YAML topology configuration: loading + strict validation (stdlib dataclasses + PyYAML)."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import ConfigError
from .names import IFNAME_RE, LINK_NAME_RE, NODE_KEY_RE

ROLES = (
    "correspondent-node",
    "home-agent",
    "mobile-node",
    "access-network-a",
    "access-network-b",
)


def _fmt(x: float) -> str:
    return f"{x:g}"


def _check_keys(d, allowed, ctx):
    if not isinstance(d, dict):
        raise ConfigError(f"{ctx}: expected a mapping, got {type(d).__name__}")
    extra = set(d) - set(allowed)
    if extra:
        raise ConfigError(f"{ctx}: unknown field(s) {sorted(extra)}")


def _number(value, lo, hi, ctx, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{ctx}: expected a number, got {value!r}")
    if integer and int(value) != value:
        raise ConfigError(f"{ctx}: expected an integer, got {value!r}")
    if not (lo <= value <= hi):
        raise ConfigError(f"{ctx}: {value} outside allowed range [{lo}, {hi}]")
    return int(value) if integer else float(value)


@dataclass(frozen=True)
class NetemSpec:
    """Impairments applied to a link end's egress with `tc qdisc ... netem`."""

    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_percent: float = 0.0
    rate_mbit: float | None = None
    limit_packets: int | None = None

    def __post_init__(self):
        _number(self.delay_ms, 0, 10_000, "delay_ms")
        _number(self.jitter_ms, 0, 10_000, "jitter_ms")
        _number(self.loss_percent, 0, 100, "loss_percent")
        if self.rate_mbit is not None:
            _number(self.rate_mbit, 0.001, 100_000, "rate_mbit")
        if self.limit_packets is not None:
            _number(self.limit_packets, 1, 1_000_000, "limit_packets", integer=True)
        if self.jitter_ms > 0 and self.delay_ms == 0:
            raise ConfigError("jitter_ms requires delay_ms > 0")

    @classmethod
    def from_dict(cls, d, ctx="netem") -> "NetemSpec":
        _check_keys(d, ("delay_ms", "jitter_ms", "loss_percent", "rate_mbit", "limit_packets"), ctx)
        return cls(**d)

    def is_empty(self) -> bool:
        return not (
            self.delay_ms or self.jitter_ms or self.loss_percent
            or self.rate_mbit is not None or self.limit_packets is not None
        )

    def to_args(self) -> list[str]:
        """Arguments placed after `netem` on the tc command line."""
        args: list[str] = []
        if self.delay_ms:
            args += ["delay", f"{_fmt(self.delay_ms)}ms"]
            if self.jitter_ms:
                args.append(f"{_fmt(self.jitter_ms)}ms")
        if self.loss_percent:
            args += ["loss", f"{_fmt(self.loss_percent)}%"]
        if self.rate_mbit is not None:
            args += ["rate", f"{_fmt(self.rate_mbit)}mbit"]
        if self.limit_packets is not None:
            args += ["limit", str(self.limit_packets)]
        return args


@dataclass(frozen=True)
class Node:
    key: str
    role: str
    forwarding: bool


@dataclass(frozen=True)
class LinkEnd:
    node: str
    ifname: str
    address: ipaddress.IPv4Interface
    netem: NetemSpec | None = None


@dataclass(frozen=True)
class Link:
    name: str
    a: LinkEnd
    b: LinkEnd
    netem: NetemSpec | None = None

    def ends(self):
        return (("a", self.a), ("b", self.b))

    def effective_netem(self, end: LinkEnd) -> NetemSpec:
        return end.netem or self.netem or NetemSpec()


@dataclass(frozen=True)
class Route:
    node: str
    to: str  # "default" or a normalised IPv4 network
    via: ipaddress.IPv4Address
    metric: int | None = None


@dataclass(frozen=True)
class TopologyConfig:
    name: str
    nodes: dict
    links: tuple
    routes: tuple

    def node_by_role(self, role: str) -> Node:
        return next(n for n in self.nodes.values() if n.role == role)

    def link(self, name: str) -> Link:
        for l in self.links:
            if l.name == name:
                return l
        raise ConfigError(f"unknown link {name!r}; known: {[l.name for l in self.links]}")

    def interfaces_of(self, node_key: str):
        out = []
        for l in self.links:
            for _, end in l.ends():
                if end.node == node_key:
                    out.append((l, end))
        return out

    def addresses_of(self, node_key: str):
        return [e.address.ip for _, e in self.interfaces_of(node_key)]


def _parse_end(d, ctx) -> LinkEnd:
    _check_keys(d, ("node", "ifname", "address", "netem"), ctx)
    for k in ("node", "ifname", "address"):
        if k not in d:
            raise ConfigError(f"{ctx}: missing '{k}'")
    if not isinstance(d["node"], str) or not NODE_KEY_RE.fullmatch(d["node"]):
        raise ConfigError(f"{ctx}: invalid node key {d['node']!r}")
    if not isinstance(d["ifname"], str) or not IFNAME_RE.fullmatch(d["ifname"]):
        raise ConfigError(f"{ctx}: invalid interface name {d['ifname']!r} (max 15 chars, [a-z0-9-])")
    try:
        addr = ipaddress.IPv4Interface(d["address"])
    except ValueError as e:
        raise ConfigError(f"{ctx}: bad address {d['address']!r}: {e}") from None
    netem = NetemSpec.from_dict(d["netem"], f"{ctx}.netem") if d.get("netem") is not None else None
    return LinkEnd(d["node"], d["ifname"], addr, netem)


def parse_config(data) -> TopologyConfig:
    _check_keys(data, ("version", "name", "nodes", "links", "routes"), "topology")
    if data.get("version") != 1:
        raise ConfigError("topology: 'version' must be 1")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError("topology: 'name' is required")

    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, dict) or not raw_nodes:
        raise ConfigError("topology: 'nodes' must be a non-empty mapping")
    nodes = {}
    for key, nd in raw_nodes.items():
        if not isinstance(key, str) or not NODE_KEY_RE.fullmatch(key):
            raise ConfigError(f"nodes: invalid node key {key!r} (pattern [a-z][a-z0-9]{{0,9}})")
        _check_keys(nd, ("role", "forwarding"), f"nodes.{key}")
        role = nd.get("role")
        if role not in ROLES:
            raise ConfigError(f"nodes.{key}: role must be one of {list(ROLES)}")
        fwd = nd.get("forwarding", False)
        if not isinstance(fwd, bool):
            raise ConfigError(f"nodes.{key}: 'forwarding' must be true/false")
        nodes[key] = Node(key, role, fwd)
    roles = sorted(n.role for n in nodes.values())
    if roles != sorted(ROLES):
        raise ConfigError(f"nodes: each of the roles {list(ROLES)} must appear exactly once")

    links = []
    names_seen, ifaces_seen, subnets_seen = set(), set(), []
    for i, ld in enumerate(data.get("links") or []):
        ctx = f"links[{i}]"
        _check_keys(ld, ("name", "a", "b", "netem"), ctx)
        lname = ld.get("name")
        if not isinstance(lname, str) or not LINK_NAME_RE.fullmatch(lname):
            raise ConfigError(f"{ctx}: invalid link name {lname!r}")
        if lname in names_seen:
            raise ConfigError(f"{ctx}: duplicate link name {lname!r}")
        names_seen.add(lname)
        a = _parse_end(ld.get("a"), f"{ctx}.a")
        b = _parse_end(ld.get("b"), f"{ctx}.b")
        for e in (a, b):
            if e.node not in nodes:
                raise ConfigError(f"{ctx}: unknown node {e.node!r}")
            if (e.node, e.ifname) in ifaces_seen:
                raise ConfigError(f"{ctx}: interface {e.ifname!r} used twice on node {e.node!r}")
            ifaces_seen.add((e.node, e.ifname))
        if a.node == b.node:
            raise ConfigError(f"{ctx}: both ends are on node {a.node!r}")
        if a.address.network != b.address.network or a.address.ip == b.address.ip:
            raise ConfigError(f"{ctx}: ends must have different addresses in the same subnet")
        if a.address.network.prefixlen > 30:
            raise ConfigError(f"{ctx}: subnet too small (use /30 or larger)")
        for other in subnets_seen:
            if a.address.network.overlaps(other):
                raise ConfigError(f"{ctx}: subnet {a.address.network} overlaps another link")
        subnets_seen.append(a.address.network)
        netem = NetemSpec.from_dict(ld["netem"], f"{ctx}.netem") if ld.get("netem") is not None else None
        links.append(Link(lname, a, b, netem))

    cfg_links = tuple(links)
    routes = []
    seen_routes = set()
    for i, rd in enumerate(data.get("routes") or []):
        ctx = f"routes[{i}]"
        _check_keys(rd, ("node", "to", "via", "metric"), ctx)
        node = rd.get("node")
        if node not in nodes:
            raise ConfigError(f"{ctx}: unknown node {node!r}")
        to = rd.get("to")
        if to != "default":
            try:
                to = str(ipaddress.IPv4Network(to, strict=True))
            except (ValueError, TypeError) as e:
                raise ConfigError(f"{ctx}: bad destination {to!r}: {e}") from None
        try:
            via = ipaddress.IPv4Address(rd.get("via"))
        except ValueError as e:
            raise ConfigError(f"{ctx}: bad gateway {rd.get('via')!r}: {e}") from None
        metric = rd.get("metric")
        if metric is not None:
            metric = _number(metric, 1, 65535, f"{ctx}.metric", integer=True)
        own = [e for l in cfg_links for _, e in l.ends() if e.node == node]
        if not any(via in e.address.network and via != e.address.ip for e in own):
            raise ConfigError(f"{ctx}: gateway {via} is not on a directly connected subnet of node {node!r}")
        ident = (node, to, str(via))
        if ident in seen_routes:
            raise ConfigError(f"{ctx}: duplicate route")
        seen_routes.add(ident)
        routes.append(Route(node, to, via, metric))

    return TopologyConfig(name, nodes, cfg_links, tuple(routes))


def load_config(path) -> TopologyConfig:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from None
    return parse_config(data)
