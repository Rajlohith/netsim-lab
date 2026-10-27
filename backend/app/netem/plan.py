"""Pure command builders. Nothing here executes anything."""
from __future__ import annotations

from dataclasses import dataclass

from .names import ns_name
from .topology_config import NetemSpec, TopologyConfig


@dataclass(frozen=True)
class Cmd:
    argv: tuple
    desc: str = ""
    privileged: bool = True   # needs root (sudo -n when the app runs unprivileged)
    readonly: bool = False    # never mutates anything


def ns_list() -> Cmd:
    # Reads /run/netns; needs no privilege.
    return Cmd(("ip", "netns", "list"), "list namespaces", privileged=False, readonly=True)


def ns_add(ns: str) -> Cmd:
    return Cmd(("ip", "netns", "add", ns), f"create namespace {ns}")


def ns_del(ns: str) -> Cmd:
    return Cmd(("ip", "netns", "del", ns), f"delete namespace {ns}")


def shape_cmd(ns: str, ifname: str, spec: NetemSpec | None) -> Cmd:
    """Set (replace) or clear the netem qdisc on one interface *inside a project namespace*."""
    if spec is None or spec.is_empty():
        return Cmd(("ip", "netns", "exec", ns, "tc", "qdisc", "del", "dev", ifname, "root"),
                   f"clear netem on {ns}:{ifname}")
    return Cmd(
        ("ip", "netns", "exec", ns, "tc", "qdisc", "replace", "dev", ifname, "root", "netem",
         *spec.to_args()),
        f"netem on {ns}:{ifname} ({' '.join(spec.to_args())})",
    )


def ping_cmd(ns: str, dst: str, count: int = 5, interval: float = 0.2, wait: int = 2) -> Cmd:
    return Cmd(
        ("ip", "netns", "exec", ns, "ping", "-n", "-c", str(count), "-i", f"{interval:g}",
         "-W", str(wait), dst),
        f"ping {dst} from {ns}",
    )


def status_cmds(ns: str) -> list[Cmd]:
    return [
        Cmd(("ip", "-n", ns, "-br", "addr", "show"), f"addresses in {ns}", readonly=True),
        Cmd(("ip", "-n", ns, "route", "show"), f"routes in {ns}", readonly=True),
        Cmd(("ip", "netns", "exec", ns, "sysctl", "-n", "net.ipv4.ip_forward"),
            f"forwarding flag in {ns}", readonly=True),
        Cmd(("ip", "netns", "exec", ns, "tc", "qdisc", "show"), f"qdiscs in {ns}", readonly=True),
    ]


def build_create_plan(cfg: TopologyConfig, prefix: str) -> list[Cmd]:
    ns = {k: ns_name(prefix, k) for k in cfg.nodes}
    plan: list[Cmd] = []

    # 1. namespaces + namespace-local sysctls (never host sysctls: `ip netns exec` scopes them)
    for key, node in cfg.nodes.items():
        plan.append(ns_add(ns[key]))
        plan.append(Cmd(("ip", "-n", ns[key], "link", "set", "lo", "up"), f"lo up in {ns[key]}"))
        plan.append(Cmd(("ip", "netns", "exec", ns[key], "sysctl", "-w", "net.ipv4.conf.all.rp_filter=0"),
                        f"rp_filter off in {ns[key]}"))
        plan.append(Cmd(("ip", "netns", "exec", ns[key], "sysctl", "-w", "net.ipv4.conf.default.rp_filter=0"),
                        f"rp_filter(default) off in {ns[key]}"))
        flag = "1" if node.forwarding else "0"
        plan.append(Cmd(("ip", "netns", "exec", ns[key], "sysctl", "-w", f"net.ipv4.ip_forward={flag}"),
                        f"ip_forward={flag} in {ns[key]}"))

    # 2. veth pairs (created directly inside the namespaces, never in the host namespace)
    for link in cfg.links:
        a, b = link.a, link.b
        plan.append(Cmd(
            ("ip", "-n", ns[a.node], "link", "add", a.ifname, "type", "veth", "peer", "name", b.ifname,
             "netns", ns[b.node]),
            f"veth {link.name}: {ns[a.node]}:{a.ifname} <-> {ns[b.node]}:{b.ifname}",
        ))
        for end in (a, b):
            plan.append(Cmd(("ip", "-n", ns[end.node], "addr", "add", str(end.address), "dev", end.ifname),
                            f"addr {end.address} on {ns[end.node]}:{end.ifname}"))
            plan.append(Cmd(("ip", "-n", ns[end.node], "link", "set", end.ifname, "up"),
                            f"{ns[end.node]}:{end.ifname} up"))

    # 3. routes (always inside a namespace)
    for r in cfg.routes:
        argv = ["ip", "-n", ns[r.node], "route", "add", r.to, "via", str(r.via)]
        if r.metric is not None:
            argv += ["metric", str(r.metric)]
        plan.append(Cmd(tuple(argv), f"route {r.to} via {r.via} in {ns[r.node]}"))

    # 4. netem (egress shaping, per link end)
    for link in cfg.links:
        for _, end in link.ends():
            spec = link.effective_netem(end)
            if not spec.is_empty():
                c = shape_cmd(ns[end.node], end.ifname, spec)
                plan.append(Cmd(tuple(("ip", "netns", "exec", ns[end.node], "tc", "qdisc", "add", "dev",
                                       end.ifname, "root", "netem", *spec.to_args())), c.desc))
    return plan


def link_set_cmd(ns: str, ifname: str, up: bool) -> Cmd:
    """Bring an interface up/down INSIDE a project namespace (used to simulate a handoff)."""
    return Cmd(("ip", "-n", ns, "link", "set", ifname, "up" if up else "down"),
               f"{ns}:{ifname} {'up' if up else 'down'}")


def launch_cmd(ns: str, uid: int, gid: int, python: str, backend_dir: str, module: str, args) -> Cmd:
    """Run a measurement tool inside a namespace with privileges dropped to the calling user.

    `ip netns exec` needs root, so the tool is started through setpriv which immediately drops
    to uid/gid: the Python process never runs as root.
    """
    return Cmd(
        ("ip", "netns", "exec", ns, "setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--",
         "env", f"PYTHONPATH={backend_dir}", python, "-m", module, *[str(a) for a in args]),
        f"launch {module} in {ns} as uid {uid}",
    )
