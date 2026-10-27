"""Our hand-built packets against the REAL Linux network stack (raw sockets on loopback).

Validates the builder direction: the kernel accepts what we build and enforces the checksums we compute. Kernel-built
loopback packets cannot be used to validate our PARSER's checksums (loopback checksum offload leaves them partial).
Needs CAP_NET_RAW (root, or `unshare -Urn`)."""
import socket
import struct
import time
import unittest

from app.compare import tcplite as T
from app.mobileip import ipip


def _raw_ok():
    try:
        socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW).close()
        return None
    except OSError as e:
        return f"raw sockets unavailable ({e})"


SKIP = _raw_ok()
LO = "127.0.0.1"


@unittest.skipIf(SKIP is not None, SKIP or "")
class KernelAcceptsOurPackets(unittest.TestCase):
    def setUp(self):
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
        self.addCleanup(self.tx.close)

    def send(self, pkt):
        self.tx.sendto(pkt, (LO, 0))

    def test_udp_over_ipv4_is_delivered_and_a_corrupt_checksum_is_dropped(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(u.close)
        u.bind((LO, 0))
        u.settimeout(0.5)
        port = u.getsockname()[1]
        good = ipip.build_udp_ip(LO, LO, 40000, port, b"hello-kernel")
        self.send(good)
        self.assertEqual(u.recvfrom(2048)[0], b"hello-kernel")
        bad = bytearray(good)
        bad[26] ^= 0xFF                                           # UDP checksum field
        self.send(bytes(bad))
        with self.assertRaises(socket.timeout):
            u.recvfrom(2048)
        self.send(good[:-3])                                      # total length lies about the data
        with self.assertRaises(socket.timeout):
            u.recvfrom(2048)

    def test_stage3_probe_datagram_survives_the_real_stack(self):
        from app.traffic import wire
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(u.close)
        u.bind((LO, 0))
        u.settimeout(0.5)
        probe = wire.encode(wire.TYPE_PROBE, 7, 12345, 111, 222, 96)
        self.send(ipip.build_udp_ip(LO, LO, 40000, u.getsockname()[1], probe))
        self.assertEqual(wire.decode(u.recvfrom(2048)[0]).seq, 12345)

    def test_tcp_syn_gets_a_syn_ack_and_a_bad_tcp_checksum_gets_nothing(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind((LO, 0))
        srv.listen(4)
        sp = srv.getsockname()[1]
        cap = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP)
        self.addCleanup(cap.close)
        cap.settimeout(0.3)

        def replies(seg):
            self.send(ipip.build_ipv4(LO, LO, 6, bytes(seg)))
            out, t0 = [], time.time()
            while time.time() - t0 < 0.6:
                try:
                    d = cap.recvfrom(4096)[0]
                except socket.timeout:
                    break
                _, pl = ipip.parse_ipv4(d)
                sport, dport, _seq, ack, _off, flags = struct.unpack("!HHIIBB", pl[:14])
                if sport == sp and dport == 45678:
                    out.append((flags, ack))
            return out
        seg = T.build_segment(LO, LO, 45678, sp, 1000, 0, T.SYN)
        self.assertEqual(replies(seg), [(T.SYN | T.ACK, 1001)])
        bad = bytearray(seg)
        bad[16] ^= 0xFF
        self.assertEqual(replies(bad), [])

    def test_ip_header_checksum_and_ipip_outer_header_equal_the_kernels(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_RAW, 4)
        self.addCleanup(rx.close)
        rx.settimeout(0.5)
        inner = ipip.build_udp_ip("10.10.1.2", "10.10.100.2", 40000, 5005, b"x" * 64)
        outer = ipip.encapsulate(inner, LO, LO, ident=0x1234)
        garbled = bytearray(outer)
        garbled[10:12] = b"\xde\xad"                              # raw IP_HDRINCL: the kernel must recompute this field
        self.send(bytes(garbled))
        seen = rx.recvfrom(4096)[0]
        self.assertEqual(seen, outer)                             # byte-for-byte: same checksum, lengths, flags, protocol 4
        h, payload = ipip.decapsulate(seen)
        self.assertEqual((h.protocol, ipip.parse_ipv4(payload)[0].ttl), (4, 63))


class KnownLimitations(unittest.TestCase):
    def test_this_kernel_cannot_run_ipip_tunnels(self):
        """Documents WHY Mobile IP tunnels are only emulated in the development sandbox (informational, not a pass/fail of our code)."""
        import os
        import subprocess
        import sys
        if os.geteuid() != 0:
            self.skipTest("probe needs root")
        code = ("import socket,struct,errno\n"
                "def a(t,d):\n n=4+len(d);return struct.pack('HH',n,t)+d+b'\\0'*((4-n%4)%4)\n"
                "info=a(1,b'ipip\\0');p=struct.pack('BxHiII',0,0,0,0,0)+a(3,b't0\\0')+a(18,info)\n"
                "m=struct.pack('IHHII',16+len(p),16,1|4|0x400|0x200,1,0)+p\n"
                "s=socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,0);s.bind((0,0));s.send(m)\n"
                "print(-struct.unpack('i',s.recv(4096)[16:20])[0])\n")
        r = subprocess.run(["unshare", "-Urn", sys.executable, "-c", code], capture_output=True, text=True)
        self.assertIn(r.stdout.strip(), {"0", "95", "19", "2"}, r)          # 0 = supported; 95/19/2 = ENOTSUP/ENODEV/ENOENT
        self.skipTest(f"ipip link creation result code {r.stdout.strip()} (0 means this kernel CAN do tunnels)")


if __name__ == "__main__":
    unittest.main()
