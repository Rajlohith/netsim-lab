"""Tiny discrete-event IPv4 network emulator (SIMPLIFIED EMULATION, not the Linux kernel).

Packets are real IPv4/UDP byte strings (built/parsed with ipip.py); routing is longest-prefix match with
metrics over *up* interfaces; links are point-to-point with a fixed delay, an up/down state and (Stage 7, off unless the
topology/conditions ask for it) seeded independent random packet loss per direction. There is no
queueing, bandwidth limit, jitter, ARP, ICMP or fragmentation. Time is virtual, so runs are exactly repeatable.
"""
from __future__ import annotations

import heapq
import ipaddress
import random
from dataclasses import dataclass, field

from . import ipip

EPOCH_WALL_NS = 1_792_746_000_000_000_000        # fixed wall-clock origin (2026-10-23T09:00:00Z): reproducible output
MONO_ORIGIN_NS = 10_000_000_000


class SimClock:
    def __init__(self):
        self.mono = MONO_ORIGIN_NS

    def mono_ns(self):
        return self.mono

    def wall_ns(self):
        return EPOCH_WALL_NS + (self.mono - MONO_ORIGIN_NS)


class SkewedClock:
    """A node whose wall clock is off by skew_ns (monotonic time is unaffected)."""

    def __init__(self, base: SimClock, skew_ns: int):
        self.base, self.skew = base, skew_ns

    def mono_ns(self):
        return self.base.mono_ns()

    def wall_ns(self):
        return self.base.wall_ns() + self.skew


class Sim:
    def __init__(self):
        self.clock = SimClock()
        self._heap, self._n = [], 0
        self.nodes, self.links = {}, {}
        self.drops: list = []                      # (mono_ns, node, reason)
        self.drop_counts: dict = {}
        self.loss_seed: int = 0                    # Stage 7: seed of the per-link loss streams (only used when a link has loss > 0)

    def at(self, t_ns: int, fn):
        self._n += 1
        heapq.heappush(self._heap, (t_ns, self._n, fn))

    def after(self, d_ns: int, fn):
        self.at(self.clock.mono + d_ns, fn)

    STORM_LIMIT = 200_000          # events at one virtual instant before we call it a bug

    def run_until(self, t_ns: int):
        same_instant, last = 0, None
        while self._heap and self._heap[0][0] <= t_ns:
            t, _, fn = heapq.heappop(self._heap)
            self.clock.mono = max(self.clock.mono, t)
            same_instant = same_instant + 1 if self.clock.mono == last else 0
            last = self.clock.mono
            if same_instant > self.STORM_LIMIT:
                raise RuntimeError(f"event storm: {self.STORM_LIMIT} events at the same virtual instant ({last} ns); "
                                   "a timer is probably being re-armed without time advancing")
            fn()
        self.clock.mono = max(self.clock.mono, t_ns)

    def drop(self, node: str, reason: str):
        self.drops.append((self.clock.mono, node, reason))
        self.drop_counts[(node, reason)] = self.drop_counts.get((node, reason), 0) + 1


@dataclass
class SimLink:
    name: str
    a: tuple                  # (node, ifname)
    b: tuple
    delay_ns: int
    up: bool = True                 # carrier: can frames cross the link?
    mtu: int = 1500
    keep_up_ends: set = field(default_factory=set)   # nodes whose end stays administratively UP while the carrier is down
    # Stage 7: independent random loss per direction (probability 0..1 per packet that is actually put on the wire).
    # Default 0 -> no random numbers are drawn and behaviour is byte-identical to Stages 1-6.
    loss_a_to_b: float = 0.0
    loss_b_to_a: float = 0.0
    seed: int = 0
    _rng: dict = field(default_factory=dict, repr=False)

    def lose(self, from_a: bool) -> bool:
        """Draw the loss decision for one packet entering the link. One dedicated, seeded stream per (link, direction), so
        the outcome does not depend on what happens on other links or in other processes (random.Random(str) is stable)."""
        p = self.loss_a_to_b if from_a else self.loss_b_to_a
        if p <= 0.0:
            return False
        key = "ab" if from_a else "ba"
        r = self._rng.get(key)
        if r is None:
            r = self._rng[key] = random.Random(f"netsim-loss:{self.seed}:{self.name}:{key}")
        return r.random() < p

    def go_down(self, keep_up_ends=()):
        """keep_up_ends=() : both ends lose address+routes (old emulator behaviour).
        keep_up_ends={'ana'}: Linux veth semantics when only the OTHER end is set down: the peer keeps its address and
        connected route but frames can no longer cross (verified on a real kernel, tests/test_live_kernel_lab.py)."""
        self.up = False
        self.keep_up_ends = set(keep_up_ends)

    def go_up(self):
        self.up = True
        self.keep_up_ends = set()


@dataclass
class SimRoute:
    net: ipaddress.IPv4Network
    ifname: str
    metric: int = 0


class SimNode:
    def __init__(self, sim: Sim, name: str, forwarding: bool = False):
        self.sim, self.name, self.forwarding = sim, name, forwarding
        self.ifaces: dict = {}                      # ifname -> (IPv4Interface, SimLink)
        self.lo_addrs: set = set()                  # extra local addresses (home address, agent address)
        self.routes: list = []
        self.udp: dict = {}                         # dport -> handler(node, src_ip, sport, dst_ip, payload)
        self.ipip_handler = None                    # handler(node, outer_header, packet) for protocol 4
        self.proto_handlers: dict = {}              # extra IP protocols: proto -> handler(node, header, payload)
        self.strong_host = False                    # True: a packet whose source is an interface address leaves via that interface
        self.forward_hook = None                    # hook(node, header, packet) -> True if consumed
        sim.nodes[name] = self

    # ---- configuration -------------------------------------------------------------------------
    def add_iface(self, ifname, addr: ipaddress.IPv4Interface, link: SimLink):
        self.ifaces[ifname] = (addr, link)

    def add_route(self, net, ifname, metric=0):
        self.routes.append(SimRoute(ipaddress.IPv4Network(net), ifname, metric))

    def iface_up(self, ifname):
        """Administratively usable for routing/addresses (carrier is checked separately when transmitting)."""
        l = self.ifaces[ifname][1]
        return l.up or self.name in l.keep_up_ends

    def local_addresses(self):
        s = set(self.lo_addrs)
        s.update(a.ip for n, (a, l) in self.ifaces.items() if self.iface_up(n))
        return s

    def lookup(self, dst):
        best = None
        cands = [SimRoute(a.network, n, 0) for n, (a, l) in self.ifaces.items() if self.iface_up(n)] + \
                [r for r in self.routes if self.iface_up(r.ifname)]
        for r in cands:
            if dst in r.net:
                key = (r.net.prefixlen, -r.metric)
                if best is None or key > best[0]:
                    best = (key, r)
        return best[1] if best else None

    # ---- data path -----------------------------------------------------------------------------
    def lookup_via(self, dst, ifname):
        best = None
        cands = [SimRoute(a.network, n, 0) for n, (a, l) in self.ifaces.items() if self.iface_up(n) and n == ifname] + \
                [r for r in self.routes if r.ifname == ifname and self.iface_up(r.ifname)]
        for r in cands:
            if dst in r.net:
                key = (r.net.prefixlen, -r.metric)
                if best is None or key > best[0]:
                    best = (key, r)
        return best[1] if best else None

    def send_ip(self, packet: bytes):
        h, _ = ipip.parse_ipv4(packet)
        r = None
        if self.strong_host:
            for n, (a, l) in self.ifaces.items():
                if a.ip == h.src and l.up:
                    r = self.lookup_via(h.dst, n)
                    break
        r = r or self.lookup(h.dst)
        if r is None:
            return self.sim.drop(self.name, "no_route")
        self._transmit(packet, r.ifname)

    def send_udp(self, src, dst, sport, dport, payload, ttl=64):
        self.send_ip(ipip.build_udp_ip(src, dst, sport, dport, payload, ttl=ttl))

    def _transmit(self, packet, ifname):
        addr, link = self.ifaces[ifname]
        if not link.up:
            return self.sim.drop(self.name, "link_down")
        if len(packet) > link.mtu:
            return self.sim.drop(self.name, "mtu_exceeded")
        from_a = link.a == (self.name, ifname)
        peer_node, peer_if = link.b if from_a else link.a
        if link.lose(from_a):
            return self.sim.drop(self.name, "link_loss")

        def arrive():
            if not link.up:
                return self.sim.drop(peer_node, "link_down_in_flight")
            self.sim.nodes[peer_node].receive(packet, peer_if)
        self.sim.after(link.delay_ns, arrive)

    def receive(self, packet: bytes, ifname: str):
        try:
            h, payload = ipip.parse_ipv4(packet)
        except ipip.IPError:
            return self.sim.drop(self.name, "bad_ip")
        if h.dst in self.local_addresses():
            return self._deliver(h, packet, payload)
        if not self.forwarding:
            return self.sim.drop(self.name, "not_for_us")
        if self.forward_hook and self.forward_hook(self, h, packet):
            return
        try:
            packet = ipip.decrement_ttl(packet)
        except ipip.IPError:
            return self.sim.drop(self.name, "ttl_expired")
        r = self.lookup(h.dst)
        if r is None:
            return self.sim.drop(self.name, "no_route")
        self._transmit(packet, r.ifname)

    def _deliver(self, h, packet, payload):
        if h.protocol == ipip.PROTO_IPIP:
            if self.ipip_handler is None:
                return self.sim.drop(self.name, "ipip_unsupported")
            return self.ipip_handler(self, h, packet)
        if h.protocol == ipip.PROTO_UDP:
            try:
                sport, dport, data = ipip.parse_udp(h.src, h.dst, payload)
            except ipip.IPError:
                return self.sim.drop(self.name, "bad_udp")
            fn = self.udp.get(dport)
            if fn is None:
                return self.sim.drop(self.name, "no_listener")
            return fn(self, h.src, sport, h.dst, data)
        fn = self.proto_handlers.get(h.protocol)
        if fn is not None:
            return fn(self, h, payload)
        self.sim.drop(self.name, "unsupported_protocol")


def build_from_topology(sim: Sim, cfg) -> dict:
    """Instantiate nodes/links/routes from a Stage 2 TopologyConfig (same addresses and link delays)."""
    nodes = {k: SimNode(sim, k, n.forwarding) for k, n in cfg.nodes.items()}
    for l in cfg.links:
        delay = int(l.effective_netem(l.a).delay_ms * 1e6)
        link = SimLink(l.name, (l.a.node, l.a.ifname), (l.b.node, l.b.ifname), delay,
                       loss_a_to_b=l.effective_netem(l.a).loss_percent / 100.0,
                       loss_b_to_a=l.effective_netem(l.b).loss_percent / 100.0,
                       seed=getattr(sim, "loss_seed", 0))
        sim.links[l.name] = link
        nodes[l.a.node].add_iface(l.a.ifname, l.a.address, link)
        nodes[l.b.node].add_iface(l.b.ifname, l.b.address, link)
    for r in cfg.routes:
        node = nodes[r.node]
        egress = next(n for n, (a, _l) in node.ifaces.items() if r.via in a.network)
        net = "0.0.0.0/0" if r.to == "default" else r.to
        node.add_route(net, egress, r.metric or 0)
    return nodes
