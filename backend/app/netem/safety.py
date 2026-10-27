"""Allow-list for every command the project may run.

`validate_argv` is called by the Runner before *every* execution. Anything outside the
grammar below raises SafetyError and is never run. In particular it is impossible to
express: iptables/nft/firewall changes, host routes (incl. the default route), host
interface changes, or touching a namespace that is not <prefix><node-key>.
"""
from __future__ import annotations

import ipaddress
import re

from .errors import SafetyError
from .names import IFNAME_RE, is_project_ns

_NETEM_TOKEN = re.compile(r"^\d+(\.\d+)?(ms|%|mbit)?$")
_NETEM_KEYWORDS = {"delay", "loss", "rate", "limit"}
_SYSCTLS = {
    ("-w", "net.ipv4.ip_forward=0"), ("-w", "net.ipv4.ip_forward=1"),
    ("-n", "net.ipv4.ip_forward"),
    ("-w", "net.ipv4.conf.all.rp_filter=0"), ("-w", "net.ipv4.conf.default.rp_filter=0"),
}


def _fail(argv, why):
    raise SafetyError(f"command rejected ({why}): {' '.join(map(str, argv))}")


def _ns(argv, name, prefix):
    if not is_project_ns(name, prefix):
        _fail(argv, f"namespace {name!r} is not a project namespace (<{prefix}><key>)")


def _ifname(argv, name, allow_lo=False):
    if not (isinstance(name, str) and (IFNAME_RE.fullmatch(name) or (allow_lo and name == "lo"))):
        _fail(argv, f"bad interface name {name!r}")


def _ip(argv, text):
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        _fail(argv, f"bad IPv4 address {text!r}")


def _cidr(argv, text):
    try:
        ipaddress.IPv4Interface(text)
    except ValueError:
        _fail(argv, f"bad CIDR {text!r}")


def _tnl(argv, name):
    if not (isinstance(name, str) and _TNL.fullmatch(name)):
        _fail(argv, f"bad tunnel device name {name!r} (must be tnl-<name>)")


def _host32(argv, text):
    try:
        ok = ipaddress.IPv4Network(text, strict=True).prefixlen == 32
    except ValueError:
        ok = False
    if not ok:
        _fail(argv, f"expected a /32 host route/address, got {text!r}")


def _route_to(argv, text):
    if text != "default":
        try:
            ipaddress.IPv4Network(text, strict=True)
        except ValueError:
            _fail(argv, f"bad route destination {text!r}")


_ABS = re.compile(r"^/[A-Za-z0-9_./-]+$")
_ARG = re.compile(r"^[A-Za-z0-9_./:=,@+-]+$")
_TOOLS = {"app.traffic.sender", "app.traffic.receiver", "app.mobileip.runtime"}
_TNL = re.compile(r"^tnl-[a-z0-9-]{1,10}$")


def _validate_launch(argv, inner):
    """setpriv --reuid=N --regid=N --clear-groups -- env PYTHONPATH=/abs /abs/python -m <tool> args..."""
    ok = (len(inner) >= 11 and re.fullmatch(r"--reuid=\d{1,10}", inner[1]) and
          re.fullmatch(r"--regid=\d{1,10}", inner[2]) and inner[3:6] == ["--clear-groups", "--", "env"])
    if not ok:
        _fail(argv, "setpriv usage not in allow-list")
    if int(inner[1].split("=")[1]) == 0:
        _fail(argv, "refusing to launch measurement tools as uid 0")
    if not (inner[6].startswith("PYTHONPATH=") and _ABS.fullmatch(inner[6][len("PYTHONPATH="):])):
        _fail(argv, "bad PYTHONPATH")
    if not _ABS.fullmatch(inner[7]) or inner[8] != "-m" or inner[9] not in _TOOLS:
        _fail(argv, "only app.traffic.sender/receiver may be launched")
    for tok in inner[10:]:
        if not _ARG.fullmatch(tok):
            _fail(argv, f"unsafe argument {tok!r}")


def _validate_inner(argv, inner, prefix):
    if not inner:
        _fail(argv, "nothing to execute in namespace")
    tool = inner[0]
    if tool == "sysctl":
        if len(inner) != 3 or (inner[1], inner[2]) not in _SYSCTLS:
            _fail(argv, "sysctl not in allow-list")
    elif tool == "tc":
        if inner[1:3] == ["qdisc", "show"] and len(inner) in (3, 5):
            if len(inner) == 5:
                if inner[3] != "dev":
                    _fail(argv, "malformed tc show")
                _ifname(argv, inner[4])
        elif len(inner) >= 7 and inner[1] == "qdisc" and inner[2] in ("add", "replace") \
                and inner[3] == "dev" and inner[5:7] == ["root", "netem"]:
            _ifname(argv, inner[4])
            for tok in inner[7:]:
                if tok not in _NETEM_KEYWORDS and not _NETEM_TOKEN.fullmatch(tok):
                    _fail(argv, f"bad netem token {tok!r}")
        elif len(inner) == 6 and inner[1:4] == ["qdisc", "del", "dev"] and inner[5] == "root":
            _ifname(argv, inner[4])
        else:
            _fail(argv, "tc usage not in allow-list")
    elif tool == "setpriv":
        _validate_launch(argv, inner)
    elif tool == "ping":
        if len(inner) != 9 or inner[1] != "-n" or inner[2] != "-c" or inner[4] != "-i" or inner[6] != "-W":
            _fail(argv, "ping usage not in allow-list")
        if not re.fullmatch(r"\d{1,4}", inner[3]) or not (1 <= int(inner[3]) <= 1000):
            _fail(argv, "bad ping count")
        if not re.fullmatch(r"\d+(\.\d+)?", inner[5]) or not (0.01 <= float(inner[5]) <= 10):
            _fail(argv, "bad ping interval")
        if not re.fullmatch(r"\d{1,2}", inner[7]):
            _fail(argv, "bad ping wait")
        _ip(argv, inner[8])
    else:
        _fail(argv, f"tool {tool!r} not allowed inside a namespace")


def validate_argv(argv, prefix: str) -> None:
    a = list(argv)
    if not a or any((not isinstance(x, str)) or x == "" or "\x00" in x for x in a):
        _fail(a, "empty or non-string argument")
    if a[0] != "ip" or len(a) < 3:
        _fail(a, "only the 'ip' command is allowed")

    if a[1:3] == ["netns", "list"] and len(a) == 3:
        return
    if a[1:3] in (["netns", "add"], ["netns", "del"]):
        if len(a) != 4:
            _fail(a, "malformed netns command")
        _ns(a, a[3], prefix)
        return
    if a[1:3] == ["netns", "exec"]:
        if len(a) < 5:
            _fail(a, "malformed netns exec")
        _ns(a, a[3], prefix)
        _validate_inner(a, a[4:], prefix)
        return

    if a[1] == "-n":
        _ns(a, a[2], prefix)
        rest = a[3:]
        if rest and rest[0] in ("-br", "-o"):
            rest = rest[1:]
        if len(rest) < 2:
            _fail(a, "malformed ip command")
        obj, verb = rest[0], rest[1]
        if obj == "link":
            if verb == "show" and len(rest) == 2:
                return
            if verb == "set" and len(rest) == 4 and rest[3] in ("up", "down"):
                _ifname(a, rest[2], allow_lo=True)
                if rest[3] == "down" and rest[2] == "lo":
                    _fail(a, "refusing to bring lo down")
                return
            if verb == "add" and len(rest) == 10 and rest[3:7] == ["type", "veth", "peer", "name"] \
                    and rest[8] == "netns":
                _ifname(a, rest[2])
                _ifname(a, rest[7])
                _ns(a, rest[9], prefix)
                return
        elif obj == "addr":
            if verb == "show" and len(rest) == 2:
                return
            if verb == "add" and len(rest) == 5 and rest[3] == "dev":
                _cidr(a, rest[2])
                _ifname(a, rest[4])
                return
            if verb == "del" and len(rest) == 5 and rest[3] == "dev" and rest[4] == "lo":
                _host32(a, rest[2])
                return
        elif obj == "tunnel":
            if verb in ("add", "change") and len(rest) == 9 and rest[3:6] == ["mode", "ipip", "local"] \
                    and rest[7] == "remote":
                _tnl(a, rest[2])
                _ip(a, rest[6])
                _ip(a, rest[8])
                return
            if verb == "del" and len(rest) == 3:
                _tnl(a, rest[2])
                return
        elif obj == "route":
            if verb == "replace" and len(rest) == 5 and rest[3] == "dev":
                _host32(a, rest[2])
                _tnl(a, rest[4])
                return
            if verb == "del" and len(rest) == 3:
                _host32(a, rest[2])
                return
            if verb == "show" and len(rest) == 2:
                return
            if verb == "get" and len(rest) == 3:
                _ip(a, rest[2])
                return
            if verb == "add" and len(rest) in (5, 7) and rest[3] == "via":
                _route_to(a, rest[2])
                _ip(a, rest[4])
                if len(rest) == 7:
                    if rest[5] != "metric" or not re.fullmatch(r"\d{1,5}", rest[6]):
                        _fail(a, "bad route metric")
                return
        _fail(a, "ip -n usage not in allow-list")
    _fail(a, "command not in allow-list")
