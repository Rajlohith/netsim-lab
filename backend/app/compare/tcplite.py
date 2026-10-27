"""TcpLite: a SIMPLIFIED TCP-like transport MODEL for the emulator (NOT the Linux TCP stack, NOT RFC 9293).

Why it exists: the emulator carries IP packets only, so TCP behaviour has to be modelled to compare TCP
reconnection with the other approaches under identical, deterministic path conditions. The application logic that
runs on top of it (apps.py) is separately validated over REAL kernel TCP sockets (loopback.py).

Real TCP wire layout (20-byte header, pseudo-header checksum) so per-packet overhead is realistic.
Modelled:   3-way handshake with SYN/SYN-ACK retransmission, byte sequence numbers, cumulative ACKs, in-order delivery,
            retransmission timeout per RFC 6298 (SRTT/RTTVAR, Karn's rule, exponential back-off) with Linux-like
            min RTO 200 ms and a configurable retry limit, RST for unknown connections, abort.
NOT modelled: congestion control, flow control/window scaling, SACK, fast retransmit, delayed ACK, Nagle, FIN
            handshake / TIME_WAIT, keepalives, PMTU discovery, out-of-order buffering (go-back-N via RTO).
Defaults are chosen for a small low-rate workload; they are parameters, not claims about any OS.
"""
from __future__ import annotations

import ipaddress
import struct
from collections import deque

from . import events  # noqa: F401  (package import order)
from ..mobileip import ipip

FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10
_HDR = struct.Struct("!HHIIBBHHH")
HEADER = 20


def build_segment(src, dst, sport, dport, seq, ack, flags, payload=b"", window=65535):
    s, d = ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed
    hdr = _HDR.pack(sport, dport, seq & 0xFFFFFFFF, ack & 0xFFFFFFFF, 5 << 4, flags, window, 0, 0)
    pseudo = s + d + struct.pack("!BBH", 0, 6, HEADER + len(payload))
    c = ipip.checksum(pseudo + hdr + payload)
    return hdr[:16] + struct.pack("!H", c) + hdr[18:] + payload


def parse_segment(src, dst, data):
    if len(data) < HEADER:
        raise ValueError("short segment")
    s, d = ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed
    if ipip.checksum(s + d + struct.pack("!BBH", 0, 6, len(data)) + data) != 0:
        raise ValueError("bad TCP checksum")
    sport, dport, seq, ack, off, flags, win, _c, _u = _HDR.unpack_from(data)
    return sport, dport, seq, ack, flags, data[(off >> 4) * 4:]


class TcpConn:
    def __init__(self, stack, local, remote, passive):
        self.stack, self.local, self.remote = stack, local, remote
        self.state = "SYN_RCVD" if passive else "SYN_SENT"
        self.iss = stack.next_iss()
        self.snd_una = self.snd_nxt = self.iss
        self.rcv_nxt = 0
        self.unacked = deque()                    # [seq, data, first_sent_ns, retransmitted]
        self.srtt = self.rttvar = None
        self.rto = stack.initial_rto
        self.retries, self._gen, self._timer_on = 0, 0, False
        self.on_connected = self.on_data = self.on_closed = None
        self.opened_ns = None
        self.stats = {"retransmissions": 0}

    @property
    def local_ip(self):
        return self.local[0]

    # ---- output ---------------------------------------------------------------------------------
    def _tx(self, seq, flags, payload=b"", kind=None):
        if self.local[0] not in self.stack.node.local_addresses():
            # like a real stack (EADDRNOTAVAIL): a socket cannot send from an address the host no longer has
            self.stack.stats["send_failures"] = self.stack.stats.get("send_failures", 0) + 1
            return self.stack.sim.drop(self.stack.node.name, "src_address_not_local")
        pkt = ipip.build_ipv4(self.local[0], self.remote[0], 6,
                              build_segment(self.local[0], self.remote[0], self.local[1], self.remote[1], seq,
                                            self.rcv_nxt, flags, payload))
        self.stack.stats["segments_tx"] += 1
        self.stack.stats["bytes_tx"] += len(pkt)
        if self.stack.seg_observer and kind:
            self.stack.seg_observer("tx", kind, len(pkt))
        self.stack.node.send_ip(pkt)

    def _arm(self):
        self._gen += 1
        self._timer_on = True
        g = self._gen
        self.stack.sim.after(int(self.rto * 1e9), lambda: self._timeout(g))

    def _disarm(self):
        self._gen += 1
        self._timer_on = False

    def _timeout(self, g):
        if g != self._gen or self.state in ("CLOSED",):
            return
        self._timer_on = False
        limit = self.stack.syn_retries if self.state in ("SYN_SENT", "SYN_RCVD") else self.stack.max_retries
        self.retries += 1
        if self.retries > limit:
            return self._close("timeout")
        self.rto = min(self.rto * 2, self.stack.max_rto)
        self.stack.stats["retransmissions"] += 1
        self.stats["retransmissions"] += 1
        if self.state == "SYN_SENT":
            self._tx(self.iss, SYN, kind="syn")
        elif self.state == "SYN_RCVD":
            self._tx(self.iss, SYN | ACK)
        elif self.unacked:
            seg = self.unacked[0]
            seg[3] = True
            self._tx(seg[0], ACK | PSH, seg[1])
        self._arm()

    def send(self, data: bytes):
        if self.state != "ESTABLISHED":
            return False
        for i in range(0, len(data), self.stack.mss):
            chunk = data[i:i + self.stack.mss]
            self.unacked.append([self.snd_nxt, chunk, self.stack.sim.clock.mono, False])
            self._tx(self.snd_nxt, ACK | PSH, chunk)
            self.snd_nxt += len(chunk)
        if self.unacked and not self._timer_on:
            self._arm()
        return True

    # ---- input ----------------------------------------------------------------------------------
    def rx(self, seq, ack, flags, payload):
        self.stack.stats["segments_rx"] += 1
        if self.stack.seg_observer and flags & SYN:
            self.stack.seg_observer("rx", "synack" if flags & ACK else "syn", HEADER + 20)
        if flags & RST:
            return self._close("reset")
        if self.state == "SYN_SENT":
            if flags & SYN and flags & ACK and ack == self.iss + 1:
                self.rcv_nxt, self.snd_una, self.snd_nxt = seq + 1, ack, ack
                self._establish()
                self._tx(self.snd_nxt, ACK, kind="handshake_ack")
            return
        if self.state == "SYN_RCVD":
            if flags & SYN:                                  # peer retransmitted its SYN: repeat our SYN-ACK
                return self._tx(self.iss, SYN | ACK)
            if flags & ACK and ack == self.iss + 1:
                self.snd_una = self.snd_nxt = ack
                self._establish()
                if self.stack.accept_cb.get(self.local[1]):
                    self.stack.accept_cb[self.local[1]](self)
            else:
                return
        if self.state != "ESTABLISHED":
            return
        if flags & ACK:
            self._ack(ack)
        if payload:
            if seq == self.rcv_nxt:
                self.rcv_nxt += len(payload)
                self._tx(self.snd_nxt, ACK)
                if self.on_data:
                    self.on_data(payload)
            else:
                self._tx(self.snd_nxt, ACK)                  # duplicate/out-of-order: re-ACK what we expect

    def _establish(self):
        self._disarm()
        self.state, self.retries, self.opened_ns = "ESTABLISHED", 0, self.stack.sim.clock.mono
        self.rto = self.stack.initial_rto if self.srtt is None else self.rto
        if self.stack.observer:
            self.stack.observer("conn_opened", self, "")
        if self.on_connected:
            self.on_connected(self)

    def _ack(self, ack):
        now = self.stack.sim.clock.mono
        advanced = False
        while self.unacked and self.unacked[0][0] + len(self.unacked[0][1]) <= ack:
            seq, data, sent, retx = self.unacked.popleft()
            advanced = True
            self.snd_una = seq + len(data)
            if not retx:                                     # Karn's rule
                r = (now - sent) / 1e9
                if self.srtt is None:
                    self.srtt, self.rttvar = r, r / 2
                else:
                    self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
                    self.srtt = 0.875 * self.srtt + 0.125 * r
                self.rto = min(max(self.srtt + 4 * self.rttvar, self.stack.min_rto), self.stack.max_rto)
        if advanced:
            self.retries = 0
            self._disarm()
            if self.unacked:
                self._arm()

    # ---- teardown -------------------------------------------------------------------------------
    def _close(self, reason):
        if self.state == "CLOSED":
            return
        self.state = "CLOSED"
        self._disarm()
        self.stack.conns.pop((self.local, self.remote), None)
        if self.stack.observer:
            self.stack.observer("conn_closed", self, reason)
        if self.on_closed:
            self.on_closed(reason)

    def abort(self):
        if self.state == "ESTABLISHED":
            self._tx(self.snd_nxt, RST | ACK)
        self.state = "CLOSED"
        self._disarm()
        self.stack.conns.pop((self.local, self.remote), None)
        if self.stack.observer:
            self.stack.observer("conn_closed", self, "aborted by application")


class TcpLiteStack:
    def __init__(self, node, sim, *, mss=1400, initial_rto=1.0, min_rto=0.2, max_rto=8.0, max_retries=6,
                 syn_retries=5, observer=None, seg_observer=None):
        self.node, self.sim, self.mss = node, sim, mss
        self.initial_rto, self.min_rto, self.max_rto = initial_rto, min_rto, max_rto
        self.max_retries, self.syn_retries, self.observer = max_retries, syn_retries, observer
        self.seg_observer = seg_observer
        self.conns, self.accept_cb = {}, {}
        self._iss, self._port = 1000, 40000
        self.stats = {"segments_tx": 0, "segments_rx": 0, "bytes_tx": 0, "retransmissions": 0, "resets_sent": 0}
        node.proto_handlers[6] = self._rx

    def next_iss(self):
        self._iss += 64000
        return self._iss

    def listen(self, port, on_accept):
        self.accept_cb[port] = on_accept

    def connect(self, local_ip, remote_ip, remote_port) -> TcpConn:
        self._port += 1
        c = TcpConn(self, (ipaddress.IPv4Address(local_ip), self._port), (ipaddress.IPv4Address(remote_ip), remote_port), False)
        self.conns[(c.local, c.remote)] = c
        c._tx(c.iss, SYN, kind="syn")
        c.snd_nxt = c.iss + 1
        c._arm()
        return c

    def _rx(self, node, h, payload):
        try:
            sport, dport, seq, ack, flags, data = parse_segment(h.src, h.dst, payload)
        except ValueError:
            return self.sim.drop(node.name, "bad_tcp")
        key = ((h.dst, dport), (h.src, sport))
        c = self.conns.get(key)
        if c is None:
            if flags & SYN and not flags & ACK and dport in self.accept_cb:
                c = TcpConn(self, key[0], key[1], True)
                c.rcv_nxt = seq + 1
                self.conns[key] = c
                c._tx(c.iss, SYN | ACK)
                c.snd_nxt = c.iss + 1
                c._arm()
            elif not flags & RST:
                self.stats["resets_sent"] += 1                    # segment for an unknown connection
                pkt = ipip.build_ipv4(h.dst, h.src, 6, build_segment(h.dst, h.src, dport, sport, ack, seq + len(data), RST | ACK))
                node.send_ip(pkt)
            return
        c.rx(seq, ack, flags, data)
