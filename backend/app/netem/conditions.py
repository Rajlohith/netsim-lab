"""Stage 7: experiment network conditions, applied ON TOP of a loaded topology (the YAML file is never edited).

A `NetworkConditions` object is a small, serialisable description of how a run differs from the plain topology file:

  latency_scale / link_delay_ms   one-way link delays (absolute per-link override first, then the global scale)
  ha_distance_ms                  EXTRA one-way delay added to every link that touches the Home Agent (cn-ha, ha-ana, ha-anb):
                                  "the Home Agent is further away". Paths that never traverse the Home Agent are unaffected.
  loss_percent / loss_links       independent random packet loss per direction on the named links (default: the two access
                                  links, ana-mn and anb-mn). Applied to EVERY packet on those links: data, echoes and signalling.
  registration_loss_percent       extra independent loss applied only to Mobile IP registration messages (MN->HA requests and
                                  HA->MN replies), on top of any link loss. Only the Mobile IP based approaches have such messages.
  ha_processing_ms                time the Home Agent needs to process a registration request.
  seed                            seeds every random draw above (loss streams are per link and direction).

The default object changes nothing: apply() then returns an equal configuration and no random number is ever drawn.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .errors import ConfigError
from .topology_config import NetemSpec, TopologyConfig

DEFAULT_LOSS_LINKS = ("ana-mn", "anb-mn")


@dataclass
class NetworkConditions:
    latency_scale: float = 1.0
    link_delay_ms: dict = field(default_factory=dict)
    ha_distance_ms: float = 0.0
    loss_percent: float = 0.0
    loss_links: tuple = DEFAULT_LOSS_LINKS
    registration_loss_percent: float = 0.0
    ha_processing_ms: float = 1.0
    seed: int = 0

    def __post_init__(self):
        self.loss_links = tuple(self.loss_links)
        self.link_delay_ms = dict(self.link_delay_ms)

    # ---- (de)serialisation -------------------------------------------------------------------------------------
    def to_json(self) -> dict:
        return {"latency_scale": self.latency_scale, "link_delay_ms": dict(sorted(self.link_delay_ms.items())),
                "ha_distance_ms": self.ha_distance_ms, "loss_percent": self.loss_percent,
                "loss_links": list(self.loss_links), "registration_loss_percent": self.registration_loss_percent,
                "ha_processing_ms": self.ha_processing_ms, "seed": self.seed}

    @classmethod
    def from_json(cls, d: dict | None) -> "NetworkConditions":
        d = dict(d or {})
        known = {"latency_scale", "link_delay_ms", "ha_distance_ms", "loss_percent", "loss_links",
                 "registration_loss_percent", "ha_processing_ms", "seed"}
        extra = set(d) - known
        if extra:
            raise ValueError(f"unknown network condition field(s) {sorted(extra)}")
        return cls(**d)

    def is_default(self) -> bool:
        return self == NetworkConditions(seed=self.seed)

    # ---- validation --------------------------------------------------------------------------------------------
    def validate(self, cfg: TopologyConfig | None = None):
        """Returns a reason string, or None when the conditions make sense (for `cfg`, if given)."""
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        if not num(self.latency_scale) or not (0 < self.latency_scale <= 100):
            return f"latency_scale must be a number in (0, 100], got {self.latency_scale!r}"
        for name, hi in (("ha_distance_ms", 5000), ("ha_processing_ms", 5000)):
            v = getattr(self, name)
            if not num(v) or not (0 <= v <= hi):
                return f"{name} must be a number in [0, {hi}], got {v!r}"
        for name in ("loss_percent", "registration_loss_percent"):
            v = getattr(self, name)
            if not num(v) or not (0 <= v < 100):
                return f"{name} must be a number in [0, 100), got {v!r}"
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or self.seed < 0:
            return f"seed must be a non-negative integer, got {self.seed!r}"
        for k, v in self.link_delay_ms.items():
            if not num(v) or not (0 <= v <= 10_000):
                return f"link_delay_ms[{k!r}] must be a number in [0, 10000], got {v!r}"
        if cfg is not None:
            names = {l.name for l in cfg.links}
            for k in list(self.link_delay_ms) + list(self.loss_links):
                if k not in names:
                    return f"unknown link {k!r}; topology links: {sorted(names)}"
            try:
                apply_conditions(cfg, self)
            except ConfigError as e:
                return f"conditions produce an invalid topology: {e}"
        return None


def _touches(cfg: TopologyConfig, link, node_key: str) -> bool:
    return link.a.node == node_key or link.b.node == node_key


def apply_conditions(cfg: TopologyConfig, cond: NetworkConditions) -> TopologyConfig:
    """A NEW TopologyConfig with the conditions applied; `cfg` is not modified. Validation of the resulting netem values
    (ranges) is done by NetemSpec itself and raises ConfigError."""
    ha = cfg.node_by_role("home-agent").key
    loss_links = set(cond.loss_links) if cond.loss_percent else set()

    def adjust(link, n: NetemSpec | None):
        if n is None:
            return None
        base = cond.link_delay_ms.get(link.name, n.delay_ms)
        delay = base * cond.latency_scale + (cond.ha_distance_ms if _touches(cfg, link, ha) else 0.0)
        loss = cond.loss_percent if link.name in loss_links else n.loss_percent
        return NetemSpec(delay_ms=round(delay, 6), jitter_ms=n.jitter_ms if delay > 0 else 0.0, loss_percent=loss,
                         rate_mbit=n.rate_mbit, limit_packets=n.limit_packets)

    links = []
    for l in cfg.links:
        # link-level netem (applies to both ends unless an end overrides it); a link with no netem at all still gets the
        # global conditions (delay 0 scaled is 0, but HA distance / loss may apply)
        base = l.netem if l.netem is not None else NetemSpec()
        links.append(replace(l, netem=adjust(l, base),
                             a=replace(l.a, netem=adjust(l, l.a.netem)), b=replace(l.b, netem=adjust(l, l.b.netem))))
    return replace(cfg, links=tuple(links))


def direct_path_rtt_ms(cfg: TopologyConfig, mn_iface: str) -> float | None:
    """DERIVED reference value: the round-trip time of the DIRECT path correspondent <-> mobile node when the mobile node is
    attached through `mn_iface`, computed from the (effective) one-way link delays: 2 * (cn-AN delay + AN-mn delay).
    None if the topology has no direct correspondent-to-access-network link for that interface. Not a measurement."""
    mn = cfg.node_by_role("mobile-node").key
    cn = cfg.node_by_role("correspondent-node").key
    access = next((l for l in cfg.links
                   if (l.a.node == mn and l.a.ifname == mn_iface) or (l.b.node == mn and l.b.ifname == mn_iface)), None)
    if access is None:
        return None
    an = access.b.node if access.a.node == mn else access.a.node
    direct = next((l for l in cfg.links if {l.a.node, l.b.node} == {cn, an}), None)
    if direct is None:
        return None
    one_way = lambda l: l.effective_netem(l.a).delay_ms
    return round(2 * (one_way(direct) + one_way(access)), 6)
