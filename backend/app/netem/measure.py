"""Parse `ping` output (iputils or busybox) into numbers."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import NetemError


@dataclass(frozen=True)
class PingStats:
    transmitted: int
    received: int
    loss_percent: float
    rtt_min_ms: float | None
    rtt_avg_ms: float | None
    rtt_max_ms: float | None


_COUNTS = re.compile(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets )?received")
_LOSS = re.compile(r"(\d+(?:\.\d+)?)%\s+packet loss")
_RTT = re.compile(r"(?:rtt|round-trip)\s+min/avg/max(?:/mdev)?\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)")


def parse_ping(text: str) -> PingStats:
    c, l = _COUNTS.search(text), _LOSS.search(text)
    if not (c and l):
        raise NetemError(f"could not parse ping output: {text.strip()[:200]!r}")
    r = _RTT.search(text)
    mn, av, mx = (float(x) for x in r.groups()) if r else (None, None, None)
    return PingStats(int(c.group(1)), int(c.group(2)), float(l.group(1)), mn, av, mx)
