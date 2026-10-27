"""Clock helpers.

* Elapsed-time measurements use time.monotonic_ns() (CLOCK_MONOTONIC on Linux): immune to wall-clock
  steps; shared by all processes (and network namespaces) of one kernel boot, NOT across machines.
* time.time_ns() is recorded next to it only to correlate runs with calendar time; never subtract it
  from another machine's wall clock and call the result a delay.
"""
from __future__ import annotations

import socket
import time
from datetime import datetime, timezone


def mono_ns() -> int:
    return time.monotonic_ns()


def wall_ns() -> int:
    return time.time_ns()


def iso_utc(ns: int) -> str:
    dt = datetime.fromtimestamp(ns // 1_000_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{(ns % 1_000_000_000) // 1000:06d}Z"


def boot_id() -> str | None:
    """Identifies the kernel boot = the CLOCK_MONOTONIC domain (None if unavailable)."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def clock_info() -> dict:
    return {
        "monotonic_source": "time.monotonic_ns (CLOCK_MONOTONIC)",
        "monotonic_resolution_ns": int(time.get_clock_info("monotonic").resolution * 1e9),
        "wall_source": "time.time_ns (CLOCK_REALTIME, may step/slew)",
        "boot_id": boot_id(),
        "hostname": socket.gethostname(),
    }
