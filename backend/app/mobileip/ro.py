"""SIMPLIFIED route-optimisation model.  *** NOT Mobile IPv6 and NOT a standards implementation. ***

Route optimisation (RO) is a feature of Mobile IPv6 (RFC 6275, section 5.2 / 11.6-11.7): after registering with its
Home Agent the mobile node tells each correspondent node its care-of address in a Binding Update, so that
correspondent can send packets directly (IPv6 routing header type 2) instead of via the Home Agent. Basic Mobile IPv4
(RFC 5944) has no RO; a separate, never widely deployed IPv4 proposal exists, and this model is NOT it.

What this model keeps from Mobile IPv6 (the performance- and security-relevant structure):
  * Return Routability: a *home test* token (reachable through the Home Agent) and a *care-of test* token (reachable
    directly at the care-of address); the binding key Kbm = SHA256(home_token | coa_token); the Binding Update is
    authenticated with Kbm, so a node that cannot receive both tokens cannot redirect somebody's traffic.
  * The CN keeps a binding cache with a lifetime and a sequence number; the MN reuses the home token while valid.
  * Direct delivery CN -> care-of address, replies MN -> CN.
What is simplified / different:
  * IPv4 transport and our own message layout (UDP port 4350); no MIPv6 mobility headers, ICMPv6, IPsec, or options.
  * The "routing header type 2 / home address option" is replaced by IP-in-IP from the CN to the care-of address
    (+20 bytes; MIPv6 uses a 24-byte routing header).
  * One nonce index for both tokens; nonces derived from the CN secret and time (stateless), 240 s lifetime, current+previous accepted.
  * Tokens are 64-bit HMAC-SHA256 truncations; the HoT is not protected against an on-path attacker on the home
    path beyond what RR assumes; no BU-to-HA IPsec protection; no CN support check/ICMP "parameter problem" fallback.
  * Handoff weakness reproduced on purpose: the CN keeps using the OLD care-of address until the new Binding Update is
    accepted (no fallback to the home path), so RO is faster in steady state but not necessarily at handoff.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import struct
from dataclasses import dataclass

from .events import NullEvents
from .home_agent import Outgoing

RO_PORT = 4350
T_HOTI, T_COTI, T_HOT, T_COT, T_BU, T_BA = 1, 2, 3, 4, 5, 6
NAMES = {1: "HoTI", 2: "CoTI", 3: "HoT", 4: "CoT", 5: "BU", 6: "BA"}
ST_ACCEPTED, ST_SEQ_OUT_OF_WINDOW, ST_AUTH_FAILED, ST_BAD_NONCE = 0, 1, 2, 3
FLAG_DEREGISTER = 1
MAC_LEN = 16
_COOKIE = struct.Struct("!B8s")
_TOKEN = struct.Struct("!B8sH8s")
_BU = struct.Struct("!BHBH4s4sH")
_BA = struct.Struct("!BBHH")


class RoError(ValueError):
    pass


@dataclass
class RoOut:
    data: bytes
    dst: tuple
    src: ipaddress.IPv4Address        # source address the mobile node must use (home address or care-of address)


def _token(secret: bytes, kind: bytes, addr: ipaddress.IPv4Address, nonce: bytes) -> bytes:
    return hmac.new(secret, kind + addr.packed + nonce, hashlib.sha256).digest()[:8]


def binding_key(home_token: bytes, coa_token: bytes) -> bytes:
    return hashlib.sha256(home_token + coa_token).digest()


def _mac(kbm: bytes, label: bytes, cn: ipaddress.IPv4Address, extra: bytes, body: bytes) -> bytes:
    return hmac.new(kbm, label + cn.packed + extra + body, hashlib.sha256).digest()[:MAC_LEN]


def encode_bu(kbm, cn, seq, flags, lifetime, home, coa, idx) -> bytes:
    body = _BU.pack(T_BU, seq, flags, lifetime, home.packed, coa.packed, idx)
    return body + _mac(kbm, b"BU", cn, coa.packed, body)


def encode_ba(kbm, cn, status, seq, lifetime) -> bytes:
    body = _BA.pack(T_BA, status, seq, lifetime)
    return body + (_mac(kbm, b"BA", cn, b"", body) if kbm else bytes(MAC_LEN))


class RoCorrespondent:
    """Correspondent-node side: token issuing (stateless), Binding Update verification, binding cache."""
    NODE = "cn"

    def __init__(self, address, clock, secret: bytes, events=None, max_lifetime_s: int = 40,
                 nonce_lifetime_s: int = 240):
        self.address = ipaddress.IPv4Address(address)
        self.clock, self.secret = clock, secret
        self.events = events or NullEvents()
        self.max_lifetime_s, self.nonce_ns = max_lifetime_s, nonce_lifetime_s * 1_000_000_000
        self.cache: dict = {}                     # home -> (coa, expires_mono_ns, seq)

    def _emit(self, kind, home="", coa="", detail=""):
        self.events.emit(self.NODE, kind, str(home), str(coa), None, None, None, detail)

    def _idx(self):
        return (self.clock.mono_ns() // self.nonce_ns) & 0xFFFF

    def _nonce(self, idx):
        return hashlib.sha256(self.secret + b"nonce" + idx.to_bytes(2, "big")).digest()[:8]

    def _valid_idx(self, idx):
        return idx in (self._idx(), (self._idx() - 1) & 0xFFFF)

    def lookup(self, home):
        e = self.cache.get(ipaddress.IPv4Address(home))
        if e is None:
            return None
        if self.clock.mono_ns() >= e[1]:
            self.cache.pop(ipaddress.IPv4Address(home), None)
            self._emit("ro_binding_removed", home, e[0], "lifetime elapsed")
            return None
        return e[0]

    def tick(self):
        for home in list(self.cache):
            self.lookup(home)

    def handle_datagram(self, data: bytes, src: tuple) -> list:
        if not data:
            return []
        t, srcip = data[0], ipaddress.IPv4Address(src[0])
        self._emit("ro_signal_received", srcip if t in (T_HOTI,) else "", srcip if t == T_COTI else "",
                   f"{NAMES.get(t, t)} {len(data)}B")
        if t in (T_HOTI, T_COTI) and len(data) == _COOKIE.size:
            cookie = _COOKIE.unpack(data)[1]
            idx = self._idx()
            kind = b"home" if t == T_HOTI else b"coa"
            tok = _token(self.secret, kind, srcip, self._nonce(idx))       # bound to the address it is sent to
            rep = _TOKEN.pack(T_HOT if t == T_HOTI else T_COT, cookie, idx, tok)
            self._emit("ro_signal_sent", detail=f"{NAMES[rep[0]]} {len(rep)}B")
            return [Outgoing(rep, (src[0], src[1]))]
        if t == T_BU and len(data) == _BU.size + MAC_LEN:
            return self._bu(data, src)
        return []

    def _bu(self, data, src):
        _t, seq, flags, life, home_b, coa_b, idx = _BU.unpack(data[:_BU.size])
        mac = data[_BU.size:]
        home, coa = ipaddress.IPv4Address(home_b), ipaddress.IPv4Address(coa_b)
        out = lambda st, k=None, lf=0: [Outgoing(encode_ba(k, self.address, st, seq, lf), (src[0], src[1]))]
        if not self._valid_idx(idx):
            self._emit("ro_rejected", home, coa, "unknown/expired nonce index")
            return self._sent(out(ST_BAD_NONCE))
        n = self._nonce(idx)
        kbm = binding_key(_token(self.secret, b"home", home, n), _token(self.secret, b"coa", coa, n))
        if not hmac.compare_digest(mac, _mac(kbm, b"BU", self.address, coa.packed, data[:_BU.size])):
            self._emit("ro_rejected", home, coa, "Binding Update failed authentication")
            return self._sent(out(ST_AUTH_FAILED))
        old = self.cache.get(home)
        if old is not None and ((seq - old[2]) & 0xFFFF) in (0,) + tuple(range(0x8000, 0x10000)):
            self._emit("ro_rejected", home, coa, f"sequence {seq} not newer than {old[2]}")
            return self._sent(out(ST_SEQ_OUT_OF_WINDOW, kbm))
        if flags & FLAG_DEREGISTER or life == 0:
            self.cache.pop(home, None)
            self._emit("ro_binding_removed", home, coa, "deregistered by BU")
            return self._sent(out(ST_ACCEPTED, kbm, 0))
        granted = min(life, self.max_lifetime_s)
        self.cache[home] = (coa, self.clock.mono_ns() + granted * 1_000_000_000, seq)
        self._emit("ro_binding_active", home, coa, f"lifetime {granted}s seq {seq}")
        return self._sent(out(ST_ACCEPTED, kbm, granted))

    def _sent(self, outs):
        for o in outs:
            self._emit("ro_signal_sent", detail=f"BA {len(o.data)}B")
        return outs


class RoMobile:
    """Mobile-node side of one correspondent: Return Routability + Binding Update state machine."""
    NODE = "mn"

    def __init__(self, home, cn_address, clock, events=None, bu_lifetime_s: int = 40,
                 home_token_lifetime_s: int = 210, retransmit_initial_s: float = 0.5, max_retransmits: int = 3,
                 refresh_fraction: float = 0.5, port: int = RO_PORT):
        self.home, self.cn = ipaddress.IPv4Address(home), ipaddress.IPv4Address(cn_address)
        self.clock, self.events = clock, events or NullEvents()
        self.bu_lifetime_s, self.home_token_ns = bu_lifetime_s, home_token_lifetime_s * 1_000_000_000
        self.rt_initial, self.max_retransmits, self.refresh_fraction = int(retransmit_initial_s * 1e9), max_retransmits, refresh_fraction
        self.port = port
        self.state, self.coa, self.active_coa = "idle", None, None
        self._home_tok = None            # (token, expires_ns)
        self._coa_tok = None
        self._idx = None
        self._cookies: dict = {}
        self._counter = 0
        self._seq = 0
        self._kbm = None
        self._deadline, self._attempt, self._refresh_at, self._last_out = None, 0, None, []
        self._bu_sent_at = None

    def _emit(self, kind, detail=""):
        self.events.emit(self.NODE, kind, str(self.home), str(self.coa or ""), None, None, None, detail)

    def _cookie(self, label):
        self._counter += 1
        return hashlib.sha256(label + self.home.packed + self._counter.to_bytes(4, "big")).digest()[:8]

    def _out(self, data, src):
        self._emit("ro_signal_sent", f"{NAMES[data[0]]} {len(data)}B")
        return RoOut(data, (str(self.cn), self.port), src)

    # ---- driving ---------------------------------------------------------------------------------
    def start(self, coa) -> list:
        """Called when the Home Agent registration for `coa` succeeded."""
        now = self.clock.mono_ns()
        self.coa, self.state, self._coa_tok, self._attempt = ipaddress.IPv4Address(coa), "rr", None, 0
        self._cookies.clear()
        outs = []
        c = self._cookie(b"coti")
        self._cookies[T_COT] = c
        outs.append(self._out(_COOKIE.pack(T_COTI, c), self.coa))
        if self._home_tok is None or now >= self._home_tok[1]:
            self._home_tok = None
            h = self._cookie(b"hoti")
            self._cookies[T_HOT] = h
            outs.append(self._out(_COOKIE.pack(T_HOTI, h), self.home))
        self._last_out = list(outs)
        self._deadline = now + self.rt_initial
        return outs

    def handle_datagram(self, data: bytes, src: tuple) -> list:
        if not data:
            return []
        t = data[0]
        self._emit("ro_signal_received", f"{NAMES.get(t, t)} {len(data)}B")
        now = self.clock.mono_ns()
        if t in (T_HOT, T_COT) and len(data) == _TOKEN.size and self.state == "rr":
            _t, cookie, idx, tok = _TOKEN.unpack(data)
            if self._cookies.get(t) != cookie:
                return []                                    # stale or forged: wrong cookie
            self._cookies.pop(t)
            if t == T_HOT:
                self._home_tok = (tok, now + self.home_token_ns)
            else:
                self._coa_tok = tok
            self._idx = idx
            if self._home_tok and self._coa_tok:
                return self._send_bu()
            return []
        if t == T_BA and len(data) == _BA.size + MAC_LEN and self.state == "bu" and self._kbm:
            _t, status, seq, life = _BA.unpack(data[:_BA.size])
            ok = hmac.compare_digest(data[_BA.size:], _mac(self._kbm, b"BA", self.cn, b"", data[:_BA.size]))
            if not ok or seq != self._seq:
                self._emit("ro_failed", "BA failed authentication or sequence mismatch; ignored")
                return []
            self._deadline = None
            if status != ST_ACCEPTED:
                self.state = "failed"
                self._emit("ro_failed", f"Binding Update refused, status {status}")
                return []
            self.state, self.active_coa = "active", self.coa
            self._refresh_at = self._bu_sent_at + int(life * self.refresh_fraction * 1e9) if life else None
            self._emit("ro_binding_active", f"lifetime {life}s")
        return []

    def _send_bu(self) -> list:
        now = self.clock.mono_ns()
        self._seq = (self._seq + 1) & 0xFFFF
        self._kbm = binding_key(self._home_tok[0], self._coa_tok)
        self.state, self._bu_sent_at, self._attempt = "bu", now, 0
        bu = encode_bu(self._kbm, self.cn, self._seq, 0, self.bu_lifetime_s, self.home, self.coa, self._idx)
        self._last_out = [self._out(bu, self.coa)]
        self._deadline = now + self.rt_initial
        return list(self._last_out)

    def poll(self) -> list:
        now = self.clock.mono_ns()
        if self.state == "active" and self._refresh_at is not None and now >= self._refresh_at:
            self._emit("ro_signal_sent", "refresh: repeating return routability")
            return self.start(self.coa)
        if self.state in ("rr", "bu") and self._deadline is not None and now >= self._deadline:
            if self._attempt >= self.max_retransmits:
                self.state, self._deadline = "failed", None
                self._emit("ro_failed", "no response from the correspondent")
                return []
            self._attempt += 1
            self._deadline = now + self.rt_initial * (2 ** self._attempt)
            return [self._out(o.data, o.src) for o in self._last_out]
        return []

    def next_deadline_ns(self):
        d = [x for x in (self._deadline if self.state in ("rr", "bu") else None,
                         self._refresh_at if self.state == "active" else None) if x is not None]
        return min(d) if d else None
