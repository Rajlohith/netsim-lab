"""Registration Request / Reply wire format, modelled on RFC 5944 section 3 (fixed parts and the
Mobile-Home Authentication Extension layout).

DEVIATIONS from RFC 5944 (so this is not a compliant implementation):
  * authenticator = HMAC-SHA256 (32 bytes) instead of the mandatory keyed-MD5 prefix+suffix mode
  * only the Mobile-Home Authentication Extension (type 32) is understood; no FA/NAI/other extensions
  * UDP port is configurable (default 4340, not 434: unprivileged tools cannot bind ports < 1024)
"""
from __future__ import annotations

import hmac
import hashlib
import ipaddress
import struct
from dataclasses import dataclass, field

TYPE_REQUEST, TYPE_REPLY = 1, 3
EXT_MH_AUTH = 32
LIFETIME_INFINITE = 0xFFFF
DEFAULT_PORT = 4340

# request flags (first byte after type)
FLAG_S, FLAG_B, FLAG_D, FLAG_M, FLAG_G, FLAG_R, FLAG_T, FLAG_X = 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01

# reply codes (RFC 5944 numbering for the ones used here)
CODE_ACCEPTED, CODE_ACCEPTED_NO_SIMULTANEOUS = 0, 1
CODE_HA_UNSPECIFIED, CODE_HA_ADMIN_PROHIBITED, CODE_HA_INSUFFICIENT_RESOURCES = 128, 129, 130
CODE_HA_MN_AUTH_FAILED, CODE_HA_IDENT_MISMATCH, CODE_HA_POORLY_FORMED, CODE_HA_UNKNOWN_HA = 131, 133, 134, 136
CODE_NAMES = {0: "accepted", 1: "accepted, simultaneous bindings unsupported", 128: "unspecified (HA)",
              129: "administratively prohibited", 130: "insufficient resources",
              131: "mobile node failed authentication", 133: "identification mismatch",
              134: "poorly formed request", 136: "unknown home agent"}


class MessageError(ValueError):
    pass


@dataclass(frozen=True)
class SecurityAssociation:
    spi: int
    key: bytes

    def __post_init__(self):
        if not (256 <= self.spi <= 0xFFFFFFFF):
            raise ValueError("SPI must be in 256..2^32-1 (0-255 are reserved by RFC 5944)")
        if len(self.key) < 16:
            raise ValueError("key must be at least 16 bytes")

    def sign(self, data: bytes) -> bytes:
        return hmac.new(self.key, data, hashlib.sha256).digest()

    def verify(self, data: bytes, authenticator: bytes) -> bool:
        return hmac.compare_digest(self.sign(data), authenticator)


@dataclass
class RegistrationRequest:
    flags: int
    lifetime: int
    home_address: ipaddress.IPv4Address
    home_agent: ipaddress.IPv4Address
    care_of_address: ipaddress.IPv4Address
    identification: int
    spi: int | None = None
    authenticator: bytes | None = None
    signed: bytes = field(default=b"", repr=False)     # bytes covered by the authenticator (decode only)


@dataclass
class RegistrationReply:
    code: int
    lifetime: int
    home_address: ipaddress.IPv4Address
    home_agent: ipaddress.IPv4Address
    identification: int
    spi: int | None = None
    authenticator: bytes | None = None
    signed: bytes = field(default=b"", repr=False)


_REQ = struct.Struct("!BBH4s4s4sQ")      # 24 bytes
_REP = struct.Struct("!BBH4s4sQ")        # 20 bytes
_EXT = struct.Struct("!BBI")             # type, length (= 4 + authenticator), SPI


def _ip(b):
    return ipaddress.IPv4Address(b)


def _with_auth(body: bytes, sa: SecurityAssociation | None) -> bytes:
    if sa is None:
        return body
    ext = _EXT.pack(EXT_MH_AUTH, 4 + 32, sa.spi)
    covered = body + ext
    return covered + sa.sign(covered)


def encode_request(req: RegistrationRequest, sa: SecurityAssociation | None = None) -> bytes:
    if not (0 <= req.lifetime <= 0xFFFF):
        raise MessageError("lifetime out of range")
    body = _REQ.pack(TYPE_REQUEST, req.flags, req.lifetime, req.home_address.packed,
                     req.home_agent.packed, req.care_of_address.packed, req.identification)
    return _with_auth(body, sa)


def encode_reply(rep: RegistrationReply, sa: SecurityAssociation | None = None) -> bytes:
    body = _REP.pack(TYPE_REPLY, rep.code, rep.lifetime, rep.home_address.packed, rep.home_agent.packed,
                     rep.identification)
    return _with_auth(body, sa)


def _parse_ext(data: bytes, offset: int):
    """Returns (spi, authenticator, signed_bytes) or (None, None, b'')."""
    if len(data) == offset:
        return None, None, b""
    if len(data) < offset + _EXT.size:
        raise MessageError("truncated extension")
    etype, elen, spi = _EXT.unpack_from(data, offset)
    if etype != EXT_MH_AUTH:
        raise MessageError(f"unsupported extension type {etype}")
    if elen < 4 + 16 or offset + 2 + elen != len(data):
        raise MessageError("bad extension length / trailing bytes")
    start = offset + _EXT.size
    return spi, data[start:], data[:start]


def decode_request(data: bytes) -> RegistrationRequest:
    if len(data) < _REQ.size or data[0] != TYPE_REQUEST:
        raise MessageError("not a registration request")
    _t, flags, life, home, ha, coa, ident = _REQ.unpack_from(data)
    spi, auth, signed = _parse_ext(data, _REQ.size)
    return RegistrationRequest(flags, life, _ip(home), _ip(ha), _ip(coa), ident, spi, auth, signed)


def decode_reply(data: bytes) -> RegistrationReply:
    if len(data) < _REP.size or data[0] != TYPE_REPLY:
        raise MessageError("not a registration reply")
    _t, code, life, home, ha, ident = _REP.unpack_from(data)
    spi, auth, signed = _parse_ext(data, _REP.size)
    return RegistrationReply(code, life, _ip(home), _ip(ha), ident, spi, auth, signed)


# ---- identification (timestamp method, RFC 5944 section 3.6.1.1: NTP 64-bit timestamp) ----------
NTP_UNIX_OFFSET = 2_208_988_800


def ident_from_wall_ns(wall_ns: int) -> int:
    secs, ns = divmod(wall_ns, 1_000_000_000)
    return ((secs + NTP_UNIX_OFFSET) << 32) | ((ns << 32) // 1_000_000_000)


def wall_ns_from_ident(ident: int) -> int:
    secs, frac = ident >> 32, ident & 0xFFFFFFFF
    return (secs - NTP_UNIX_OFFSET) * 1_000_000_000 + (frac * 1_000_000_000) // (1 << 32)
