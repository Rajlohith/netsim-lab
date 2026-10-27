"""IPv4 / UDP packet building and IP-in-IP (RFC 2003 style) encapsulation, in pure Python.

Implemented: header build/parse with checksum verification, encapsulation with inner-TTL decrement,
DF copy, TOS copy, decapsulation with validation. Not implemented: fragmentation/reassembly,
ICMP errors (e.g. "fragmentation needed"), tunnel-loop detection, minimal encapsulation, GRE.
"""
from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass

PROTO_IPIP, PROTO_UDP = 4, 17
OVERHEAD = 20                       # outer IPv4 header without options


class IPError(ValueError):
    pass


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


@dataclass(frozen=True)
class IPv4:
    tos: int
    total_length: int
    identification: int
    df: bool
    mf: bool
    frag_offset: int
    ttl: int
    protocol: int
    src: ipaddress.IPv4Address
    dst: ipaddress.IPv4Address
    header_len: int


def build_ipv4(src, dst, proto: int, payload: bytes, ttl: int = 64, tos: int = 0,
               ident: int = 0, df: bool = False) -> bytes:
    total = 20 + len(payload)
    if total > 65535:
        raise IPError("packet too large")
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, tos, total, ident & 0xFFFF, 0x4000 if df else 0,
                      ttl, proto, 0, ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed)
    return hdr[:10] + struct.pack("!H", checksum(hdr)) + hdr[12:] + payload


def parse_ipv4(data: bytes):
    if len(data) < 20:
        raise IPError("short packet")
    vihl, tos, total, ident, ff, ttl, proto, csum, src, dst = struct.unpack("!BBHHHBBH4s4s", data[:20])
    if vihl >> 4 != 4:
        raise IPError("not IPv4")
    ihl = (vihl & 0xF) * 4
    if ihl < 20 or len(data) < ihl:
        raise IPError("bad header length")
    if total < ihl or total > len(data):
        raise IPError("bad total length")
    if checksum(data[:ihl]) != 0:
        raise IPError("bad header checksum")
    h = IPv4(tos, total, ident, bool(ff & 0x4000), bool(ff & 0x2000), ff & 0x1FFF, ttl, proto,
             ipaddress.IPv4Address(src), ipaddress.IPv4Address(dst), ihl)
    return h, data[ihl:total]


def with_ttl(packet: bytes, ttl: int) -> bytes:
    h, _ = parse_ipv4(packet)
    b = bytearray(packet)
    b[8] = ttl
    b[10:12] = b"\x00\x00"
    b[10:12] = struct.pack("!H", checksum(bytes(b[:h.header_len])))
    return bytes(b)


def decrement_ttl(packet: bytes) -> bytes:
    h, _ = parse_ipv4(packet)
    if h.ttl <= 1:
        raise IPError("ttl expired")
    return with_ttl(packet, h.ttl - 1)


def encapsulate(inner: bytes, outer_src, outer_dst, ttl: int = 64, decrement_inner_ttl: bool = True,
                ident: int = 0) -> bytes:
    """RFC 2003-style: outer header (protocol 4) in front of the inner datagram, +20 bytes."""
    h, _ = parse_ipv4(inner)
    if h.mf or h.frag_offset:
        raise IPError("fragmented inner packet not supported")
    if decrement_inner_ttl:
        inner = decrement_ttl(inner)               # raises IPError("ttl expired")
    return build_ipv4(outer_src, outer_dst, PROTO_IPIP, inner[:h.total_length], ttl=ttl, tos=h.tos,
                      ident=ident, df=h.df)


def decapsulate(outer: bytes):
    """Returns (outer_header, inner_packet). Raises IPError for anything that is not a clean IP-in-IP packet."""
    h, payload = parse_ipv4(outer)
    if h.protocol != PROTO_IPIP:
        raise IPError(f"not IP-in-IP (protocol {h.protocol})")
    if h.mf or h.frag_offset:
        raise IPError("fragmented outer packet not supported")
    parse_ipv4(payload)                             # inner must itself be a valid IPv4 packet
    return h, payload


def build_udp(src, dst, sport: int, dport: int, payload: bytes) -> bytes:
    s, d = ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed
    length = 8 + len(payload)
    hdr = struct.pack("!HHHH", sport, dport, length, 0)
    c = checksum(s + d + struct.pack("!BBH", 0, PROTO_UDP, length) + hdr + payload) or 0xFFFF
    return struct.pack("!HHHH", sport, dport, length, c) + payload


def parse_udp(src, dst, data: bytes):
    if len(data) < 8:
        raise IPError("short UDP")
    sport, dport, length, csum = struct.unpack("!HHHH", data[:8])
    if length < 8 or length > len(data):
        raise IPError("bad UDP length")
    data = data[:length]
    if csum:
        s, d = ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed
        if checksum(s + d + struct.pack("!BBH", 0, PROTO_UDP, length) + data) != 0:
            raise IPError("bad UDP checksum")
    return sport, dport, data[8:]


def build_udp_ip(src, dst, sport, dport, payload, ttl=64, ident=0, df=False) -> bytes:
    return build_ipv4(src, dst, PROTO_UDP, build_udp(src, dst, sport, dport, payload), ttl=ttl,
                      ident=ident, df=df)
