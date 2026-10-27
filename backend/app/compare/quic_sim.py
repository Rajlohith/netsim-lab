"""QUIC connection migration in the emulator, using the real aioquic protocol engine (sans-IO API) in VIRTUAL time.

*** STATUS: written against aioquic's documented sans-IO API but NOT EXECUTED in the development sandbox, because
aioquic could not be installed there (no network). Treat as unverified until tests/test_compare_quic.py passes on a
machine with `pip install aioquic`. ***

What is real: handshake, packet protection, streams, loss recovery, path validation (PATH_CHALLENGE/RESPONSE) and
connection-ID handling are aioquic's. What is emulated: the network (simnet), and the OS notification that the client's
local address changed (delivered `detect_delay_ms` after the link change, like for the other approaches).
Scope (do NOT generalise): this shows client-initiated QUIC connection migration for an application written to
use QUIC. It does not make arbitrary TCP applications mobile; an unmodified TCP application gains nothing from it.
Server-side limitations of this harness: one connection, one stream, no 0-RTT, no preferred address, no retry.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import math
import ssl
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .apps import StreamProbeClient, StreamProbeServer
from .transport_lab import SERVER_PORT, TransportLab

QUIC_PORT, CLIENT_PORT, ALPN = 4433, 50000, "nsl-probe"


def quic_available() -> bool:
    return importlib.util.find_spec("aioquic") is not None


def make_self_signed(dirpath: Path):
    """Throw-away EC certificate for the in-memory server (needs `cryptography`, an aioquic dependency)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "netsim-lab.invalid")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=2)).sign(key, hashes.SHA256()))
    cp, kp = Path(dirpath) / "cert.pem", Path(dirpath) / "key.pem"
    cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    return cp, kp


class QuicStreamConn:
    """Adapts one QUIC stream to the small Conn interface used by apps.py."""

    def __init__(self, ep, stream_id=None):
        self.ep, self.stream_id = ep, stream_id
        self.on_connected = self.on_data = self.on_closed = None

    def send(self, data: bytes):
        if self.stream_id is None:
            return False
        self.ep.conn.send_stream_data(self.stream_id, data, end_stream=False)
        self.ep.flush()
        return True

    def abort(self):
        self.ep.conn.close()
        self.ep.flush()


class _Endpoint:
    def __init__(self, lab, node, conn, local_port, role):
        self.lab, self.node, self.conn, self.local_port, self.role = lab, node, conn, local_port, role
        self.local_ip = None
        self.stream_conn = None
        self._timer_at = None

    def _now(self):
        return self.lab.sim.clock.mono / 1e9

    def flush(self):
        for data, addr in self.conn.datagrams_to_send(self._now()):
            self.node.send_udp(self.local_ip, addr[0], self.local_port, addr[1], data)
        t = self.conn.get_timer()
        if t is not None and t != self._timer_at:
            self._timer_at = t
            # round UP (+1 ns): a timer that fires fractionally early is ignored by handle_timer() and would be re-armed
            # at the same virtual instant forever
            self.lab.sim.at(max(math.ceil(t * 1e9) + 1, self.lab.sim.clock.mono), self._fire)

    def _fire(self):
        self._timer_at = None
        self.conn.handle_timer(self._now())
        self.pump()
        self.flush()
        t = self.conn.get_timer()
        if t is not None and t <= self._now():             # still overdue after handling: retry shortly, never at this instant
            self._timer_at = t
            self.lab.sim.at(self.lab.sim.clock.mono + 1_000_000, self._fire)

    def on_datagram(self, src_ip, sport, data):
        self.conn.receive_datagram(data, (str(src_ip), sport), self._now())
        self.pump()
        self.flush()

    def pump(self):
        from aioquic.quic import events as qe
        while True:
            ev = self.conn.next_event()
            if ev is None:
                return
            if isinstance(ev, qe.HandshakeCompleted) and self.role == "client":
                self.stream_conn.stream_id = self.conn.get_next_available_stream_id()
                self.lab.log.emit("mn", "conn_opened", 0, "QUIC handshake completed")
                if self.stream_conn.on_connected:
                    self.stream_conn.on_connected()
            elif isinstance(ev, qe.StreamDataReceived):
                if self.role == "server" and self.stream_conn is None:
                    self.stream_conn = QuicStreamConn(self, ev.stream_id)
                    self.lab.server.on_accept(self.stream_conn)
                if self.stream_conn and self.stream_conn.on_data and ev.data:
                    self.stream_conn.on_data(ev.data)
            elif isinstance(ev, qe.ConnectionTerminated):
                self.lab.log.emit("mn" if self.role == "client" else "cn", "failure", 0,
                                  f"QUIC connection terminated: {ev.error_code} {ev.reason_phrase}")
                if self.stream_conn and self.stream_conn.on_closed:
                    self.stream_conn.on_closed("terminated")


class QuicLab(TransportLab):
    def run(self):
        try:
            return super().run()
        finally:                                          # the throw-away certificate directory
            tmp = getattr(self, "_tmp", None)
            if tmp is not None:
                tmp.cleanup()

    def build(self):
        from aioquic.buffer import Buffer
        from aioquic.quic.configuration import QuicConfiguration
        from aioquic.quic.connection import QuicConnection
        from aioquic.quic.packet import pull_quic_header
        s = self.spec
        self._tmp = tempfile.TemporaryDirectory()
        cert, key = make_self_signed(self._tmp.name)
        self.server_cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
        self.server_cfg.load_cert_chain(str(cert), str(key))
        client_cfg = QuicConfiguration(is_client=True, alpn_protocols=[ALPN], verify_mode=ssl.CERT_NONE)
        self.client_ep = _Endpoint(self, self.mn, QuicConnection(configuration=client_cfg), CLIENT_PORT, "client")
        self.server_ep = None
        n_s = s.duration_s - s.probe_start_s
        self.server = StreamProbeServer(self.sched, s.rate_pps, n_s, s.payload, self.hooks, session=self.session_id)
        self.client = StreamProbeClient(self.sched, self._connect, self.hooks, reconnect=False, session=self.session_id)

        def cn_udp(node, src, sport, dst, data):
            if self.server_ep is None:
                hdr = pull_quic_header(Buffer(data=data), host_cid_length=self.server_cfg.connection_id_length)
                conn = QuicConnection(configuration=self.server_cfg, original_destination_connection_id=hdr.destination_cid)
                self.server_ep = _Endpoint(self, self.cn, conn, QUIC_PORT, "server")
                self.server_ep.local_ip = self.cn_addr
            self.server_ep.on_datagram(src, sport, data)
        self.cn.udp[QUIC_PORT] = cn_udp
        self.mn.udp[CLIENT_PORT] = lambda node, src, sport, dst, data: self.client_ep.on_datagram(src, sport, data)
        self.sim.at(self.t0, lambda: self.client.start(self.mn_ip(s.initial_iface)))
        self.server.start(self.t0 + int(s.probe_start_s * 1e9))
        self.sim.at(self.t0 + int((s.duration_s + 0.2) * 1e9), self.client.stop)

    def _connect(self, local_ip):
        ep = self.client_ep
        ep.local_ip = ipaddress.IPv4Address(local_ip)
        ep.stream_conn = QuicStreamConn(ep)
        ep.conn.connect((str(self.cn_addr), QUIC_PORT), now=self.sim.clock.mono / 1e9)
        ep.flush()
        return ep.stream_conn

    def on_attached(self, ip):
        ep = self.client_ep
        ep.local_ip = ipaddress.IPv4Address(ip)           # packets now leave from the new address -> server sees a new path
        try:
            ep.conn.change_connection_id()                 # use a fresh connection ID on the new path (RFC 9000 9.5)
        except Exception as e:                             # no spare CID yet: continue, the server may still validate the path
            self.log.emit("mn", "note", 0, f"change_connection_id failed: {e}")
        self.client.on_network_change(str(ip))
        ep.conn.send_ping(self.sim.clock.mono & 0xFFFF)    # provoke a packet from the new address immediately
        ep.flush()

    def summary(self):
        import aioquic
        return {"mode": "emulated-aioquic",
                "overhead": {"mobility_bytes_per_packet": 0.0,
                             "basis": "no mobility-specific header; QUIC transport overhead differs from TCP and is not mobility-related"},
                "continuity": {"transport_connection_preserved": self.client.conn is not None,
                               "endpoint_address_preserved": False, "connections_opened": 1},
                "model": {"transport": f"aioquic {getattr(aioquic, '__version__', '?')} (real protocol engine, virtual time)",
                          "scope": "client-initiated migration of a QUIC application; not applicable to unmodified TCP applications"}}
