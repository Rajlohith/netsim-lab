"""Home Agent: registration processing + binding management (protocol logic only).

Implemented behaviour (RFC 5944-inspired, simplified): request validation, Mobile-Home authentication,
timestamp replay protection with the 133 resynchronisation response, lifetime negotiation (HA may
shorten), registration / re-registration / deregistration, binding expiry, data-plane install/remove.
Simplified / absent: Foreign Agents, proxy/gratuitous ARP, broadcast/multicast, simultaneous bindings,
reverse tunnelling, ICMP, extensions other than Mobile-Home auth. See docs/MOBILE_IP.md.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from . import messages as M
from .binding import BindingTable
from .dataplane import DataPlaneError, HaDataPlane
from .events import NullEvents


@dataclass
class Outgoing:
    data: bytes
    dst: tuple            # (ip str, port)


class HomeAgent:
    NODE = "ha"

    def __init__(self, agent_address, home_network, associations: dict, dataplane: HaDataPlane, clock,
                 events=None, max_lifetime_s: int = 300, replay_window_s: float = 7.0,
                 processing_delay_ns: int = 0):
        self.agent_address = ipaddress.IPv4Address(agent_address)
        self.home_network = ipaddress.IPv4Network(home_network)
        self.sa = {ipaddress.IPv4Address(k): v for k, v in associations.items()}
        self.dataplane, self.clock = dataplane, clock
        self.events = events or NullEvents()
        self.max_lifetime_s, self.replay_window_ns = max_lifetime_s, int(replay_window_s * 1e9)
        self.processing_delay_ns = processing_delay_ns        # modelled processing time (sim uses it)
        self.bindings = BindingTable()
        self._last_ident: dict = {}
        self.stats = {"requests": 0, "accepted": 0, "rejected": 0, "tunnel_packets": 0,
                      "inner_bytes": 0, "outer_bytes": 0}

    # ------------------------------------------------------------------
    def _emit(self, kind, home="", coa="", ident=None, code=None, life=None, detail=""):
        self.events.emit(self.NODE, kind, str(home), str(coa), ident, code, life, detail)

    def _reply(self, req, code, lifetime, sa, ident=None, src=None):
        rep = M.RegistrationReply(code, lifetime, req.home_address, self.agent_address,
                                  req.identification if ident is None else ident)
        data = M.encode_reply(rep, sa)
        self._emit("reg_reply_sent", req.home_address, req.care_of_address, rep.identification, code, lifetime,
                   M.CODE_NAMES.get(code, ""))
        if code >= 128:
            self.stats["rejected"] += 1
        return [Outgoing(data, src)]

    def handle_datagram(self, data: bytes, src: tuple) -> list:
        self.stats["requests"] += 1
        try:
            req = M.decode_request(data)
        except M.MessageError as e:
            self._emit("request_malformed", detail=str(e))
            if len(data) >= 24 and data[0] == M.TYPE_REQUEST:      # enough to answer 134 (unauthenticated)
                try:
                    stub = M.decode_request(data[:24])
                    return self._reply(stub, M.CODE_HA_POORLY_FORMED, 0, None, src=src)
                except M.MessageError:
                    pass
            return []
        self._emit("reg_request_received", req.home_address, req.care_of_address, req.identification, None,
                   req.lifetime, f"from {src[0]}:{src[1]}")

        sa = self.sa.get(req.home_address)
        if req.home_agent != self.agent_address:
            return self._reply(req, M.CODE_HA_UNKNOWN_HA, 0, sa, src=src)
        if sa is None or req.authenticator is None or req.spi != sa.spi or not sa.verify(req.signed, req.authenticator):
            self._emit("auth_failure", req.home_address, req.care_of_address, req.identification,
                       M.CODE_HA_MN_AUTH_FAILED, detail="unknown MN, SPI mismatch or bad authenticator")
            return self._reply(req, M.CODE_HA_MN_AUTH_FAILED, 0, sa, src=src)
        if req.home_address not in self.home_network:
            return self._reply(req, M.CODE_HA_ADMIN_PROHIBITED, 0, sa, src=src)

        # replay protection: timestamp within the window AND strictly newer than the last accepted one
        now_wall = self.clock.wall_ns()
        skew = abs(M.wall_ns_from_ident(req.identification) - now_wall)
        last = self._last_ident.get(req.home_address, -1)
        if skew > self.replay_window_ns or req.identification <= last:
            self._emit("replay_rejected", req.home_address, req.care_of_address, req.identification,
                       M.CODE_HA_IDENT_MISMATCH,
                       detail="outside time window" if skew > self.replay_window_ns else "not newer than last accepted")
            return self._reply(req, M.CODE_HA_IDENT_MISMATCH, 0, sa, ident=M.ident_from_wall_ns(now_wall), src=src)

        if not (req.flags & M.FLAG_D):
            return self._reply(req, M.CODE_HA_UNSPECIFIED, 0, sa, src=src)        # only co-located CoA supported
        if req.flags & (M.FLAG_M | M.FLAG_G | M.FLAG_T):
            return self._reply(req, M.CODE_HA_UNSPECIFIED, 0, sa, src=src)        # only plain IP-in-IP, no reverse tunnel

        home = req.home_address
        self._last_ident[home] = req.identification
        if req.lifetime == 0:                                                      # deregistration
            if req.care_of_address != home:
                return self._reply(req, M.CODE_HA_POORLY_FORMED, 0, sa, src=src)
            self._remove(home, "deregistered", kind="binding_removed")
            self.stats["accepted"] += 1
            return self._reply(req, M.CODE_ACCEPTED, 0, sa, src=src)

        coa = req.care_of_address
        if coa in self.home_network or coa.is_unspecified or coa.is_multicast or coa == self.agent_address:
            return self._reply(req, M.CODE_HA_POORLY_FORMED, 0, sa, src=src)
        granted = min(req.lifetime, self.max_lifetime_s) if req.lifetime != M.LIFETIME_INFINITE else self.max_lifetime_s
        try:
            self.dataplane.install_binding(home, coa)
        except DataPlaneError as e:
            self._emit("dataplane_error", home, coa, req.identification, M.CODE_HA_INSUFFICIENT_RESOURCES, detail=str(e))
            return self._reply(req, M.CODE_HA_INSUFFICIENT_RESOURCES, 0, sa, src=src)
        b, how = self.bindings.upsert(home, coa, granted, self.clock.mono_ns(), req.identification)
        self._emit(f"binding_{how}", home, coa, req.identification, None, granted, f"registrations={b.registrations}")
        self.stats["accepted"] += 1
        code = M.CODE_ACCEPTED_NO_SIMULTANEOUS if req.flags & M.FLAG_S else M.CODE_ACCEPTED
        return self._reply(req, code, granted, sa, src=src)

    # ------------------------------------------------------------------
    def _remove(self, home, reason, kind):
        b = self.bindings.remove(home)
        if b is not None:
            try:
                self.dataplane.remove_binding(home)
            except DataPlaneError as e:
                self._emit("dataplane_error", home, b.care_of_address, None, None, None, f"remove failed: {e}")
            self._emit(kind, home, b.care_of_address, None, None, None, reason)

    def tick(self) -> list:
        """Expire bindings whose lifetime elapsed (call regularly / at the next deadline)."""
        gone = []
        for b in self.bindings.expired(self.clock.mono_ns()):
            self._remove(b.home_address, "lifetime elapsed", kind="binding_expired")
            gone.append(b.home_address)
        return gone

    def next_deadline_ns(self):
        exp = [b.expires_mono_ns for b in self.bindings.snapshot() if b.expires_mono_ns is not None]
        return min(exp) if exp else None

    def note_tunnel_packet(self, inner_len: int, outer_len: int):
        self.stats["tunnel_packets"] += 1
        self.stats["inner_bytes"] += inner_len
        self.stats["outer_bytes"] += outer_len

    def shutdown(self):
        """Remove every binding (and its tunnel) and log tunnel statistics: idempotent cleanup."""
        for b in self.bindings.snapshot():
            self._remove(b.home_address, "home agent shutdown", kind="binding_removed")
        s = self.stats
        self._emit("tunnel_stats", detail=f"packets={s['tunnel_packets']} inner_bytes={s['inner_bytes']} "
                                          f"outer_bytes={s['outer_bytes']}")
