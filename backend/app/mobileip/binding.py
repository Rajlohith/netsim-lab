"""Home Agent binding table: home address -> care-of address with a lifetime (monotonic clock)."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from .messages import LIFETIME_INFINITE


@dataclass
class Binding:
    home_address: ipaddress.IPv4Address
    care_of_address: ipaddress.IPv4Address
    lifetime_s: int                    # granted
    created_mono_ns: int
    updated_mono_ns: int
    expires_mono_ns: int | None        # None = infinite
    identification: int
    registrations: int = 1

    def remaining_s(self, now_ns: int):
        return None if self.expires_mono_ns is None else max(0.0, (self.expires_mono_ns - now_ns) / 1e9)


class BindingTable:
    """One binding per home address (simultaneous bindings are not supported)."""

    def __init__(self):
        self._b: dict = {}

    def __len__(self):
        return len(self._b)

    def get(self, home) -> Binding | None:
        return self._b.get(ipaddress.IPv4Address(home))

    def snapshot(self) -> list:
        return list(self._b.values())

    def upsert(self, home, coa, lifetime_s: int, now_ns: int, identification: int):
        """Returns (binding, 'created' | 'updated')."""
        home, coa = ipaddress.IPv4Address(home), ipaddress.IPv4Address(coa)
        exp = None if lifetime_s == LIFETIME_INFINITE else now_ns + lifetime_s * 1_000_000_000
        old = self._b.get(home)
        if old is None:
            b = Binding(home, coa, lifetime_s, now_ns, now_ns, exp, identification)
            self._b[home] = b
            return b, "created"
        old.care_of_address, old.lifetime_s = coa, lifetime_s
        old.updated_mono_ns, old.expires_mono_ns, old.identification = now_ns, exp, identification
        old.registrations += 1
        return old, "updated"

    def remove(self, home) -> Binding | None:
        return self._b.pop(ipaddress.IPv4Address(home), None)

    def expired(self, now_ns: int) -> list:
        return [b for b in self._b.values() if b.expires_mono_ns is not None and now_ns >= b.expires_mono_ns]
