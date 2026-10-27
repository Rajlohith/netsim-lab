"""Mobile Node: registration state machine (co-located care-of address mode, no Foreign Agent).

Timers are driven by the caller through poll()/next_deadline_ns() so the same code runs on a real
UDP socket loop and inside the discrete-event simulator.
"""
from __future__ import annotations

import ipaddress

from . import messages as M
from .dataplane import DataPlaneError, MnDataPlane
from .events import NullEvents
from .home_agent import Outgoing


class MobileNode:
    NODE = "mn"

    def __init__(self, home_address, home_agent, sa: M.SecurityAssociation, clock, dataplane: MnDataPlane,
                 events=None, requested_lifetime_s: int = 60, retransmit_initial_s: float = 1.0,
                 retransmit_max_s: float = 8.0, max_retransmits: int = 3, reregister_fraction: float = 0.5,
                 agent_port: int = M.DEFAULT_PORT):
        self.home_address = ipaddress.IPv4Address(home_address)
        self.home_agent = ipaddress.IPv4Address(home_agent)
        self.sa, self.clock, self.dataplane = sa, clock, dataplane
        self.events = events or NullEvents()
        self.requested_lifetime_s = requested_lifetime_s
        self.rt_initial, self.rt_max = int(retransmit_initial_s * 1e9), int(retransmit_max_s * 1e9)
        self.max_retransmits, self.rereg_fraction = max_retransmits, reregister_fraction
        self.agent_port = agent_port
        self.state = "detached"
        self.care_of: ipaddress.IPv4Address | None = None
        self.granted_lifetime_s = 0
        self.clock_offset_ns = 0              # learnt from a 133 reply
        self._last_ident = 0
        self._pending: dict = {}              # ident -> (sent_mono_ns, lifetime, coa, dereg)
        self._attempt, self._retx_at, self._rereg_at, self._resyncs = 0, None, None, 0
        self._dereg = False
        self._cur = (0, None, False)          # (lifetime, coa, dereg) of the request being (re)tried

    # ---- helpers -------------------------------------------------------------------------------
    def _emit(self, kind, coa="", ident=None, code=None, life=None, detail=""):
        self.events.emit(self.NODE, kind, str(self.home_address), str(coa or ""), ident, code, life, detail)

    def _new_ident(self) -> int:
        i = M.ident_from_wall_ns(self.clock.wall_ns() + self.clock_offset_ns)
        self._last_ident = i if i > self._last_ident else self._last_ident + 1
        return self._last_ident

    def _send_request(self, lifetime: int, coa, dereg=False, kind="reg_request_sent", detail="") -> list:
        ident = self._new_ident()
        flags = M.FLAG_D
        req = M.RegistrationRequest(flags, lifetime, self.home_address, self.home_agent, ipaddress.IPv4Address(coa), ident)
        data = M.encode_request(req, self.sa)
        now = self.clock.mono_ns()
        self._pending[ident] = (now, lifetime, ipaddress.IPv4Address(coa), dereg)
        self._cur = (lifetime, ipaddress.IPv4Address(coa), dereg)
        wait = min(self.rt_initial * (2 ** self._attempt), self.rt_max)
        self._retx_at = now + wait
        self._emit(kind, coa, ident, None, lifetime, detail or f"attempt {self._attempt + 1}")
        return [Outgoing(data, (str(self.home_agent), self.agent_port))]

    # ---- API -----------------------------------------------------------------------------------
    def attach(self, coa) -> list:
        """Movement detected: acquired care-of address `coa` on a foreign network."""
        coa = ipaddress.IPv4Address(coa)
        self._emit("attach", coa, detail="new care-of address acquired")
        self.care_of, self.state = coa, "registering"
        self._pending.clear()
        self._attempt, self._resyncs, self._rereg_at, self._dereg = 0, 0, None, False
        try:
            self.dataplane.set_care_of(coa)                    # be able to decapsulate before the HA tunnels to us
        except DataPlaneError as e:
            self._emit("dataplane_error", coa, detail=str(e))
            self.state = "failed"
            return []
        return self._send_request(self.requested_lifetime_s, coa)

    def deregister(self) -> list:
        """Return home / leave: lifetime 0 with care-of address = home address (RFC 5944 3.6.1.2)."""
        if self.care_of is None:
            return []
        self._emit("detach", self.care_of, detail="deregistering")
        self._pending.clear()
        self._attempt, self._rereg_at, self._dereg = 0, None, True
        return self._send_request(0, self.home_address, dereg=True)

    def handle_datagram(self, data: bytes, src: tuple) -> list:
        try:
            rep = M.decode_reply(data)
        except M.MessageError as e:
            self._emit("reply_auth_invalid", detail=f"undecodable reply: {e}")
            return []
        if rep.home_address != self.home_address or rep.spi != self.sa.spi or rep.authenticator is None \
                or not self.sa.verify(rep.signed, rep.authenticator):
            self._emit("reply_auth_invalid", ident=rep.identification, code=rep.code,
                       detail="reply failed authentication; ignored")
            return []
        now = self.clock.mono_ns()
        if rep.code == M.CODE_HA_IDENT_MISMATCH:
            if not self._pending:
                return []
            self._emit("reg_rejected", self.care_of, rep.identification, rep.code, None, M.CODE_NAMES[rep.code])
            if self._resyncs >= 2:
                self.state = "failed"
                self._emit("reg_failed", self.care_of, detail="could not resynchronise identification clock")
                return []
            self._resyncs += 1
            self.clock_offset_ns = M.wall_ns_from_ident(rep.identification) - self.clock.wall_ns()
            self._last_ident = 0          # the old floor came from the wrong clock; restart from the corrected one
            self._emit("resync_clock", self.care_of, detail=f"offset {self.clock_offset_ns / 1e9:.3f}s learnt from HA")
            life, coa, dereg = self._cur
            self._pending.clear()
            return self._send_request(life, coa, dereg=dereg, detail="after clock resync")
        pend = self._pending.get(rep.identification)
        if pend is None:
            self._emit("reply_auth_invalid", ident=rep.identification, code=rep.code,
                       detail="reply does not match any outstanding request (stale/replayed); ignored")
            return []
        sent, lifetime, coa, dereg = pend
        self._emit("reg_reply_received", coa, rep.identification, rep.code, rep.lifetime,
                   f"{M.CODE_NAMES.get(rep.code, 'code ' + str(rep.code))}; rtt_ns={now - sent}")
        self._pending.clear()
        self._retx_at = None
        if rep.code in (M.CODE_ACCEPTED, M.CODE_ACCEPTED_NO_SIMULTANEOUS):
            if dereg:
                self.state, self.care_of = "detached", None
                try:
                    self.dataplane.clear()
                except DataPlaneError as e:
                    self._emit("dataplane_error", detail=str(e))
                return []
            self.state, self.granted_lifetime_s = "registered", rep.lifetime
            self._rereg_at = sent + int(rep.lifetime * self.rereg_fraction * 1e9)
            return []
        self.state = "failed"
        self._emit("reg_failed", coa, rep.identification, rep.code, None, M.CODE_NAMES.get(rep.code, ""))
        return []

    def poll(self) -> list:
        now = self.clock.mono_ns()
        if self.state == "registering" and self._retx_at is not None and now >= self._retx_at:
            if self._attempt >= self.max_retransmits:
                self.state, self._retx_at = "failed", None
                self._emit("reg_timeout", self.care_of, detail=f"no reply after {self._attempt + 1} attempts")
                self._emit("reg_failed", self.care_of, detail="timeout")
                return []
            self._attempt += 1
            self._emit("reg_retransmit", self.care_of, detail=f"attempt {self._attempt + 1}")
            life, coa, dereg = self._cur          # old idents stay valid: a late reply to them still counts
            return self._send_request(life, coa, dereg=dereg, kind="reg_request_sent")
        if self.state == "registered" and self._rereg_at is not None and now >= self._rereg_at:
            self._attempt, self._rereg_at, self.state = 0, None, "registering"
            self._emit("reregister", self.care_of, detail="binding lifetime fraction elapsed")
            return self._send_request(self.requested_lifetime_s, self.care_of, kind="reg_request_sent")
        return []

    def next_deadline_ns(self):
        d = [t for t in (self._retx_at if self.state == "registering" else None,
                         self._rereg_at if self.state == "registered" else None) if t is not None]
        return min(d) if d else None
