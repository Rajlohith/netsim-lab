"""Probe packet format (big-endian, 36-byte header, zero padded to `size`).

  0  4s  magic 'NSP1'
  4  B   version (1)
  5  B   type: 0 = probe, 1 = echo
  6  H   reserved (0)
  8  I   session_id
 12  Q   seq              (starts at 0, +1 per probe)
 20  Q   send_mono_ns     sender's CLOCK_MONOTONIC when the packet was built
 28  Q   send_wall_ns     sender's wall clock (informational)
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"NSP1"
VERSION = 1
TYPE_PROBE, TYPE_ECHO = 0, 1
_HDR = struct.Struct("!4sBBHIQQQ")
HEADER_SIZE = _HDR.size            # 36
MAX_SIZE = 1472                    # fits one Ethernet frame without IP fragmentation


class WireError(ValueError):
    pass


@dataclass(frozen=True)
class Packet:
    ptype: int
    session_id: int
    seq: int
    send_mono_ns: int
    send_wall_ns: int
    size: int


def encode(ptype: int, session_id: int, seq: int, send_mono_ns: int, send_wall_ns: int,
           size: int = HEADER_SIZE) -> bytes:
    if not (HEADER_SIZE <= size <= MAX_SIZE):
        raise WireError(f"size must be in [{HEADER_SIZE}, {MAX_SIZE}]")
    if ptype not in (TYPE_PROBE, TYPE_ECHO):
        raise WireError("bad type")
    hdr = _HDR.pack(MAGIC, VERSION, ptype, 0, session_id, seq, send_mono_ns, send_wall_ns)
    return hdr + bytes(size - HEADER_SIZE)


def decode(data: bytes) -> Packet:
    if len(data) < HEADER_SIZE:
        raise WireError(f"short datagram ({len(data)} bytes)")
    magic, ver, ptype, _res, session, seq, smono, swall = _HDR.unpack_from(data)
    if magic != MAGIC:
        raise WireError("bad magic")
    if ver != VERSION:
        raise WireError(f"unsupported version {ver}")
    if ptype not in (TYPE_PROBE, TYPE_ECHO):
        raise WireError(f"unknown type {ptype}")
    return Packet(ptype, session, seq, smono, swall, len(data))


def make_echo(data: bytes) -> bytes:
    """Same bytes with the type flipped to ECHO (timestamps untouched)."""
    decode(data)
    return data[:5] + bytes([TYPE_ECHO]) + data[6:]
