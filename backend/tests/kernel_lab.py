"""Test infrastructure: execute the project's REAL `ip` command plans on the REAL Linux kernel without iproute2.

`KernelIpExecutor` is a drop-in `executor` for app.netem.runner.Runner. It interprets exactly the argv forms that
plan.py / topology.py generate (`ip netns add|del|list`, `ip -n NS link add veth/set up|down`, `addr add`, `route add`,
`netns exec NS sysctl -w`) using rtnetlink, `os.unshare` and `os.setns`. Namespaces are held by tiny helper processes.

What this is: a faithful kernel-level check of the plans (addresses, routes, metrics, forwarding, veth, cleanup).
What it is NOT: a test of the `ip`/`tc` command-line syntax (the binaries are not installed in the dev sandbox), and it
cannot do tc netem or ip tunnels (this kernel refuses them).  Needs CAP_NET_ADMIN/SYS_ADMIN (root, or `unshare -Urn`).
"""
from __future__ import annotations

import ipaddress
import os
import signal
import socket
import struct
import subprocess
import sys

from app.netem.runner import CmdResult

NLM_F_REQUEST, NLM_F_ACK, NLM_F_EXCL, NLM_F_CREATE = 0x1, 0x4, 0x200, 0x400
RTM_NEWLINK, RTM_NEWADDR, RTM_NEWROUTE = 16, 20, 24
IFLA_IFNAME, IFLA_LINKINFO, IFLA_NET_NS_PID = 3, 18, 19
IFLA_INFO_KIND, IFLA_INFO_DATA, VETH_INFO_PEER = 1, 2, 1
IFA_ADDRESS, IFA_LOCAL = 1, 2
RTA_DST, RTA_GATEWAY, RTA_PRIORITY = 1, 5, 6
IFF_UP = 1
HOLDER = ("import os, signal, sys; os.unshare(os.CLONE_NEWNET); sys.stdout.write('ready\\n'); sys.stdout.flush(); "
          "signal.pause()")


def _attr(t: int, data: bytes) -> bytes:
    n = 4 + len(data)
    return struct.pack("HH", n, t) + data + b"\0" * ((4 - n % 4) % 4)


def _request(mtype: int, flags: int, payload: bytes):
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)       # created in the CURRENT thread's network namespace
    try:
        s.bind((0, 0))
        s.send(struct.pack("IHHII", 16 + len(payload), mtype, NLM_F_REQUEST | NLM_F_ACK | flags, 1, 0) + payload)
        resp = s.recv(8192)
    finally:
        s.close()
    err = struct.unpack("i", resp[16:20])[0]
    if err:
        raise OSError(-err, os.strerror(-err))


class Namespace:
    def __init__(self, name):
        self.name = name
        self.proc = subprocess.Popen([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE, text=True)
        if self.proc.stdout.readline().strip() != "ready":
            raise RuntimeError("namespace holder failed to start")
        self.pid = self.proc.pid
        self.fd = os.open(f"/proc/{self.pid}/ns/net", os.O_RDONLY)

    def destroy(self):
        os.close(self.fd)
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()
        self.proc.stdout.close()


class _InNs:
    def __init__(self, fd):
        self.fd, self.orig = fd, None

    def __enter__(self):
        self.orig = os.open("/proc/thread-self/ns/net", os.O_RDONLY)
        os.setns(self.fd, os.CLONE_NEWNET)

    def __exit__(self, *exc):
        os.setns(self.orig, os.CLONE_NEWNET)
        os.close(self.orig)


class KernelIpExecutor:
    def __init__(self):
        self.ns: dict = {}
        self.log: list = []

    # ---- helpers for tests ----------------------------------------------------------------------
    def in_ns(self, name):
        return _InNs(self.ns[name].fd)

    def pid(self, name):
        return self.ns[name].pid

    def interfaces(self, name) -> list:
        with self.in_ns(name):
            with open("/proc/net/dev") as f:
                return [l.split(":")[0].strip() for l in f.read().splitlines()[2:]]

    def addresses(self, name) -> dict:
        import fcntl
        out = {}
        with self.in_ns(name):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            with open("/proc/net/dev") as f:
                names = [l.split(":")[0].strip() for l in f.read().splitlines()[2:]]
            for ifn in names:
                try:
                    r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", ifn.encode()))     # SIOCGIFADDR
                    out[ifn] = socket.inet_ntoa(r[20:24])
                except OSError:
                    pass
            s.close()
        return out

    def sysctl(self, name, key) -> str:
        with self.in_ns(name):
            with open("/proc/sys/" + key.replace(".", "/")) as f:
                return f.read().strip()

    def link_state(self, name, ifname) -> dict:
        """{'admin_up': IFF_UP, 'carrier': IFF_RUNNING} read with SIOCGIFFLAGS inside the namespace."""
        import fcntl
        with self.in_ns(name):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                flags = struct.unpack("H", fcntl.ioctl(s.fileno(), 0x8913, struct.pack("256s", ifname.encode()))[16:18])[0]
            finally:
                s.close()
        return {"admin_up": bool(flags & 0x1), "carrier": bool(flags & 0x40)}

    def cleanup(self):
        for n in list(self.ns.values()):
            n.destroy()
        self.ns.clear()

    # ---- the Runner executor ------------------------------------------------------------------------
    def __call__(self, argv, timeout):
        argv = list(argv)
        self.log.append(tuple(argv))
        try:
            out = self._dispatch(argv)
            return CmdResult(tuple(argv), 0, out or "", "")
        except Exception as e:
            return CmdResult(tuple(argv), 1, "", f"{type(e).__name__}: {e}")

    def _dispatch(self, a):
        if a[1:3] == ["netns", "list"]:
            return "".join(f"{n} (id: {i})\n" for i, n in enumerate(self.ns))
        if a[1:3] == ["netns", "add"]:
            if a[3] in self.ns:
                raise FileExistsError("namespace exists")
            self.ns[a[3]] = Namespace(a[3])
            return ""
        if a[1:3] == ["netns", "del"]:
            self.ns.pop(a[3]).destroy()
            return ""
        if a[1:3] == ["netns", "exec"]:
            if a[4] == "sysctl" and a[5] == "-w":
                key, val = a[6].split("=")
                with self.in_ns(a[3]):
                    with open("/proc/sys/" + key.replace(".", "/"), "w") as f:
                        f.write(val)
                return ""
            raise NotImplementedError(f"netns exec {a[4]}: not available in the kernel lab (no tc/ping)")
        if a[1] == "-n":
            ns, rest = a[2], a[3:]
            if rest[0] == "link" and rest[1] == "set":
                return self._link_set(ns, rest[2], rest[3])
            if rest[0] == "link" and rest[1] == "add":
                return self._veth(ns, rest[2], rest[7], rest[9])
            if rest[0] == "addr" and rest[1] == "add":
                return self._addr(ns, rest[4], rest[2])
            if rest[0] == "route" and rest[1] == "add":
                return self._route(ns, rest[2], rest[4], int(rest[6]) if len(rest) > 5 else 0)
        raise NotImplementedError(" ".join(a))

    # ---- rtnetlink operations ----------------------------------------------------------------------
    def _link_set(self, ns, ifname, state):
        with self.in_ns(ns):
            idx = socket.if_nametoindex(ifname)
            flags = IFF_UP if state == "up" else 0
            _request(RTM_NEWLINK, 0, struct.pack("BxHiII", 0, 0, idx, flags, IFF_UP))
        return ""

    def _veth(self, ns_a, if_a, if_b, ns_b):
        peer = struct.pack("BxHiII", 0, 0, 0, 0, 0) + _attr(IFLA_IFNAME, if_b.encode() + b"\0") + \
            _attr(IFLA_NET_NS_PID, struct.pack("I", self.ns[ns_b].pid))
        info = _attr(IFLA_INFO_KIND, b"veth\0") + _attr(IFLA_INFO_DATA, _attr(VETH_INFO_PEER, peer))
        payload = struct.pack("BxHiII", 0, 0, 0, 0, 0) + _attr(IFLA_IFNAME, if_a.encode() + b"\0") + _attr(IFLA_LINKINFO, info)
        with self.in_ns(ns_a):
            _request(RTM_NEWLINK, NLM_F_CREATE | NLM_F_EXCL, payload)
        return ""

    def _addr(self, ns, ifname, cidr):
        ifc = ipaddress.IPv4Interface(cidr)
        with self.in_ns(ns):
            idx = socket.if_nametoindex(ifname)
            scope = 254 if ifc.ip.is_loopback or ifname == "lo" else 0
            payload = struct.pack("BBBBI", socket.AF_INET, ifc.network.prefixlen, 0, scope, idx) + \
                _attr(IFA_LOCAL, ifc.ip.packed) + _attr(IFA_ADDRESS, ifc.ip.packed)
            _request(RTM_NEWADDR, NLM_F_CREATE | NLM_F_EXCL, payload)
        return ""

    def _route(self, ns, to, via, metric):
        dst_len = 0 if to == "default" else ipaddress.IPv4Network(to).prefixlen
        payload = struct.pack("BBBBBBBBI", socket.AF_INET, dst_len, 0, 0, 254, 3, 0, 1, 0)
        if to != "default":
            payload += _attr(RTA_DST, ipaddress.IPv4Network(to).network_address.packed)
        payload += _attr(RTA_GATEWAY, ipaddress.IPv4Address(via).packed)
        if metric:
            payload += _attr(RTA_PRIORITY, struct.pack("I", metric))
        with self.in_ns(ns):
            _request(RTM_NEWROUTE, NLM_F_CREATE | NLM_F_EXCL, payload)
        return ""


def skip_reason():
    """None if the kernel lab can run here, else why not (evaluated once)."""
    if os.geteuid() != 0:
        return "needs root or `unshare -Urn` (CAP_NET_ADMIN for netlink)"
    if not hasattr(os, "unshare") or not hasattr(os, "setns"):
        return "Python >= 3.12 (os.unshare/os.setns) required"
    ex = KernelIpExecutor()
    try:
        ex(["ip", "netns", "add", "probe-a"], 5)
        ex(["ip", "netns", "add", "probe-b"], 5)
        r = ex(["ip", "-n", "probe-a", "link", "add", "pa", "type", "veth", "peer", "name", "pb", "netns", "probe-b"], 5)
        if r.returncode:
            return f"kernel cannot create veth pairs here ({r.stderr})"
        return None
    except Exception as e:
        return f"kernel lab unavailable: {type(e).__name__}: {e}"
    finally:
        ex.cleanup()


# ---- helpers to run code / processes inside a lab namespace ---------------------------------------------------
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ns_cmd(ex, ns, *argv):
    """argv prefix that runs a command inside the namespace (nsenter works because the holder process owns it)."""
    return ["nsenter", "-t", str(ex.pid(ns)), "-n", "--", *argv]


def ns_python(ex, ns, code, timeout=30):
    r = subprocess.run(ns_cmd(ex, ns, sys.executable, "-c", code), capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip(), r.stderr.strip()


ECHO_SERVER = ("import socket,sys; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind((sys.argv[1],{port})); "
               "print('up',flush=True)\nwhile True:\n    d,a=s.recvfrom(2048); s.sendto(d,a)")
CLIENT = ("import socket,sys; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(0.5); ok=0\n"
          "for i in range(5):\n    s.sendto(b'ping',(sys.argv[1],{port}))\n"
          "    try: s.recvfrom(100); ok+=1\n    except socket.timeout: pass\nprint(ok)")
_PORT = [7700]


def udp_roundtrip(ex, src_ns, dst_ns, dst_ip, bind_ip="0.0.0.0") -> int:
    """How many of 5 UDP echoes made it src_ns -> dst_ns -> src_ns across the REAL kernel lab."""
    _PORT[0] += 1
    port = _PORT[0]
    srv = subprocess.Popen(ns_cmd(ex, dst_ns, sys.executable, "-c", ECHO_SERVER.format(port=port), bind_ip),
                           stdout=subprocess.PIPE, text=True)
    try:
        srv.stdout.readline()
        out, err = ns_python(ex, src_ns, CLIENT.format(port=port) .replace("sys.argv[1]", repr(dst_ip)))
        return int(out or 0)
    finally:
        srv.kill()
        srv.wait()
        srv.stdout.close()


ICMP_PROBE = r'''
import json, socket, time, sys
dst, window = sys.argv[1], float(sys.argv[2])
icmp = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP); icmp.settimeout(0.1)
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for i in range(5): u.sendto(b"x" * 20, (dst, 9))
seen = []; t0 = time.time()
while time.time() - t0 < window:
    try: data, addr = icmp.recvfrom(2048)
    except socket.timeout: continue
    ihl = (data[0] & 15) * 4; seen.append([addr[0], data[ihl], data[ihl + 1]])
print(json.dumps(seen))
'''


def icmp_responses(ex, ns, dst_ip, window_s):
    """Send 5 UDP packets from `ns` to dst_ip and return the ICMP errors [[src, type, code], ...] that come back."""
    import json
    r = subprocess.run(ns_cmd(ex, ns, sys.executable, "-c", ICMP_PROBE, dst_ip, str(window_s)), capture_output=True, text=True)
    return json.loads(r.stdout) if r.stdout.strip() else [("error", r.stderr)]


def read_routes(ex, ns):
    """Parse /proc/net/route inside the namespace -> [(iface, dest, gateway, metric)]."""
    out = []
    with ex.in_ns(ns):
        with open("/proc/net/route") as fh:
            lines = fh.read().splitlines()[1:]
        for line in lines:
            f = line.split()
            ip = lambda h: socket.inet_ntoa(struct.pack("<I", int(h, 16)))
            out.append((f[0], ip(f[1]), ip(f[2]), int(f[6])))
    return out
