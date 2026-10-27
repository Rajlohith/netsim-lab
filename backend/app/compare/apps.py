"""The common application workload for the transport-based approaches (TCP reconnection, QUIC migration).

Same job as the UDP probes of Stage 3: the correspondent node (server) pushes sequence-numbered probes at a fixed
rate to the mobile node (client) and gets an echo per probe, over ONE reliable byte stream.
Framing: 4-byte length + payload.  H = client hello (session id, next expected seq), D = data (probe packet),
A = ack (echo packet).  The app is written against two tiny interfaces so the same code runs on
  * TcpLite in the emulator (virtual time),  * real kernel TCP sockets on loopback (loopback.py),
  * a QUIC stream (quic_sim.py, needs aioquic).
Reconnection policy (TCP baseline only): `detect="event"` = the OS tells the app about the address change
(reconnect immediately), `detect="timeout"` = the app only notices a stall after `detect_timeout_s` without data.
Resume protocol: after reconnecting, the client's HELLO carries the next sequence number it needs; the server
re-sends everything from there out of its buffer.  This is application-level continuity; the TRANSPORT connection
is NOT preserved.
"""
from __future__ import annotations

import struct

from ..traffic import wire

SESSION = 0x53455331


def frame(kind: bytes, body: bytes = b"") -> bytes:
    payload = kind + body
    return struct.pack("!I", len(payload)) + payload


class Framer:
    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> list:
        self.buf += data
        out = []
        while len(self.buf) >= 4:
            n = struct.unpack("!I", self.buf[:4])[0]
            if n > 1 << 20:
                raise ValueError("frame too large")
            if len(self.buf) < 4 + n:
                break
            out.append(self.buf[4:4 + n])
            self.buf = self.buf[4 + n:]
        return out


class StreamProbeServer:
    """Correspondent node. `sched` provides now_ns()/wall_ns()/call_later(delay_s, fn)."""

    def __init__(self, sched, rate_pps, duration_s, size, hooks, session=SESSION, t0_ns=None):
        self.sched, self.rate, self.duration, self.size, self.hooks, self.session = sched, rate_pps, duration_s, size, hooks, session
        self.buf: dict = {}                       # seq -> D frame (kept until echoed)
        self.conn = None
        self.n = int(duration_s * rate_pps)
        self.t0_ns = t0_ns
        self.sent = 0
        self.resends = 0

    def start(self, t0_ns):
        """Probe k is due at t0 + k/rate (absolute schedule: no cumulative drift)."""
        self.t0_ns = t0_ns
        self.sched.call_at(t0_ns, self._tick)

    def _due(self, seq):
        return self.t0_ns + int(seq * 1e9 / self.rate)

    def _tick(self):
        seq = self.sent
        if seq >= self.n:
            return
        mono, wall = self.sched.now_ns(), self.sched.wall_ns()
        probe = wire.encode(wire.TYPE_PROBE, self.session, seq, mono, wall, self.size)
        self.hooks["sent"](seq, self._due(seq), mono, wall, len(probe))
        fr = frame(b"D", probe)
        self.buf[seq] = fr
        self.sent += 1
        if self.conn is not None:
            self.conn.send(fr)
        self.sched.call_at(self._due(seq + 1), self._tick)

    # ---- connection events --------------------------------------------------------------------------
    def on_accept(self, conn):
        framer = Framer()
        conn.on_data = lambda data, c=conn, f=framer: self._data(c, f, data)
        conn.on_closed = lambda reason, c=conn: self._closed(c, reason)

    def _closed(self, conn, reason):
        if conn is self.conn:
            self.conn = None
            self.hooks["event"]("note", f"server connection closed: {reason}")

    def _data(self, conn, framer, data):
        try:
            frames = framer.feed(data)
        except ValueError:
            return conn.abort()
        for f in frames:
            if f[:1] == b"H" and len(f) == 13:
                sess, nxt = struct.unpack("!IQ", f[1:])
                if sess != self.session:
                    return conn.abort()
                old = self.conn
                self.conn = conn
                if old is not None and old is not conn:
                    old.abort()                          # the client moved: drop the stale connection
                    self.hooks["event"]("note", "resume: stale connection replaced")
                pending = sorted(s for s in self.buf if s >= nxt)
                for seq in pending:
                    conn.send(self.buf[seq])
                if old is not None:
                    self.resends += len(pending)          # data re-sent because a connection was replaced
            elif f[:1] == b"A":
                try:
                    p = wire.decode(f[1:])
                except wire.WireError:
                    continue
                self.hooks["echo"](p.seq, self.sched.now_ns(), self.sched.wall_ns(), len(f) - 1)
                self.buf.pop(p.seq, None)


class StreamProbeClient:
    """Mobile node."""

    def __init__(self, sched, connect_fn, hooks, *, reconnect=True, detect="event", detect_timeout_s=0.5,
                 retry_delay_s=0.1, session=SESSION):
        self.sched, self.connect_fn, self.hooks = sched, connect_fn, hooks
        self.reconnect, self.detect, self.detect_timeout_ns = reconnect, detect, int(detect_timeout_s * 1e9)
        self.retry_ns, self.session = retry_delay_s, session
        self.next_expected = 0
        self.conn, self.local_ip, self.framer = None, None, None
        self.last_activity = 0
        self.connections_opened = 0
        self.pending_resume = False
        self.stopped = False

    def start(self, local_ip):
        self.local_ip = local_ip
        self._open("initial connection")
        self.sched.call_later(self.detect_timeout_ns / 4e9, self._watchdog)

    def stop(self):
        self.stopped = True

    # ---- connection management ----------------------------------------------------------------------
    def _open(self, why):
        if self.stopped:
            return
        self.pending_resume = self.connections_opened > 0
        conn = self.connect_fn(self.local_ip)
        self.conn, self.framer = conn, Framer()
        self.last_activity = self.sched.now_ns()
        conn.on_connected = lambda c=conn: self._connected(c)
        conn.on_data = lambda d, c=conn: self._data(c, d)
        conn.on_closed = lambda reason, c=conn: self._closed(c, reason)
        self.hooks["event"]("note", f"connecting from {self.local_ip} ({why})")

    def _connected(self, conn):
        if conn is not self.conn:
            return
        self.connections_opened += 1
        self.last_activity = self.sched.now_ns()
        conn.send(frame(b"H", struct.pack("!IQ", self.session, self.next_expected)))
        self.hooks["signal_tx"](13 + 4 + 1)
        self.hooks["event"]("conn_opened", f"from {self.local_ip}, resume_from={self.next_expected}")

    def on_network_change(self, new_ip):
        """The OS tells the app its address changed (after movement detection)."""
        self.local_ip = new_ip
        if not self.reconnect:                                  # connection migration: the same connection continues
            self.pending_resume = True
            self.hooks["event"]("note", f"network change: continuing on the same connection from {new_ip}")
        elif self.detect == "event":
            self._reconnect("network change notification")

    def _reconnect(self, why):
        if self.stopped or not self.reconnect:
            return
        old, self.conn = self.conn, None
        if old is not None:
            old.on_closed = None
            old.abort()
        self.hooks["event"]("conn_closed", why)
        self._open(why)

    def _closed(self, conn, reason):
        if conn is not self.conn or self.stopped:
            return
        self.conn = None
        self.hooks["event"]("conn_closed", f"connection lost: {reason}")
        if self.reconnect:
            self.sched.call_later(self.retry_ns, lambda: self._open("retry after " + reason) if self.conn is None else None)
        else:
            self.hooks["event"]("failure", f"connection lost and reconnection is not supported: {reason}")

    def _watchdog(self):
        if self.stopped:
            return
        if self.reconnect and self.detect == "timeout" and self.conn is not None and \
                self.sched.now_ns() - self.last_activity > self.detect_timeout_ns:
            self._reconnect(f"no data for {self.detect_timeout_ns / 1e9:g}s (stall detected)")
        self.sched.call_later(self.detect_timeout_ns / 4e9, self._watchdog)

    # ---- data ---------------------------------------------------------------------------------------
    def _data(self, conn, data):
        if conn is not self.conn:
            return
        self.last_activity = self.sched.now_ns()
        try:
            frames = self.framer.feed(data)
        except ValueError:
            return conn.abort()
        for f in frames:
            if f[:1] != b"D":
                continue
            try:
                p = wire.decode(f[1:])
            except wire.WireError:
                continue
            dup = p.seq < self.next_expected
            self.hooks["recv"](p, len(f) - 1, dup)
            if p.seq >= self.next_expected:
                self.next_expected = p.seq + 1
            if self.pending_resume and not dup:
                self.pending_resume = False
                self.hooks["event"]("data_resumed", f"first data on the new connection (seq {p.seq})")
            conn.send(frame(b"A", wire.make_echo(f[1:])))
