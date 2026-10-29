"""Approach registry: what each approach is, what it is NOT, and where it can run."""
from __future__ import annotations

from dataclasses import dataclass

ENV_LABELS = {"emulated": "emulated (virtual time)", "loopback": "loopback (real sockets)", "live": "live namespaces (sudo)"}


@dataclass(frozen=True)
class ApproachInfo:
    id: str
    name: str
    status: str                  # what standard (if any) this corresponds to, and how close
    mechanism: str
    recovery_point: str          # what "recovered" means for the handoff-latency metric
    mobility_overhead: str
    scope_limits: str


APPROACHES = {a.id: a for a in (
    ApproachInfo(
        "tcp-reconnect", "TCP reconnection baseline",
        "No mobility protocol. Application-level behaviour over TCP (RFC 9293). In the emulator TCP is the simplified "
        "TcpLite model; the same application logic is validated over real kernel TCP on loopback.",
        "The address change kills the TCP connection. The app detects it (OS notification, or a stall timeout), opens a new "
        "connection from the new address and resumes the session with its own sequence numbers (HELLO carries the next needed seq).",
        "first new (non-duplicate) data frame delivered to the mobile node on the NEW connection",
        "none specific to mobility (the peer sees a new address and a new connection)",
        "Needs an application that implements resume; an unmodified TCP application simply loses its connection."),
    ApproachInfo(
        "mip-basic", "Basic Mobile IP",
        "Educational Mobile IPv4-style demonstration (Stage 4). NOT RFC 5944 compliant: HMAC-SHA256 instead of keyed-MD5, "
        "no Foreign Agent, no ARP, no reverse tunnelling (see docs/MOBILE_IP.md).",
        "Registration with the Home Agent (signed, replay-protected); the Home Agent tunnels (IP-in-IP) packets for the "
        "home address to the current care-of address; replies go direct (triangle routing).",
        "first accepted registration reply (the Home Agent now tunnels to the new care-of address)",
        "20 bytes of IP-in-IP header per packet on the forward path (via the Home Agent)",
        "Break-before-make: the old link disappears with the movement."),
    ApproachInfo(
        "mip-mbb", "Mobile IP with make-before-break handoff",
        "Basic Mobile IP (above) with an overlap period: the new link is up before the old one is released. A testbed "
        "extension, not a separate protocol; the live demo uses a single tunnel device so in-flight packets for the old "
        "care-of address are not accepted there (the emulator accepts both addresses during the overlap).",
        "Registration is sent from the new care-of address while the old link still carries traffic; the old link is released "
        "`old_link_hold_ms` after registration so packets already in flight can still arrive.",
        "first accepted registration reply",
        "20 bytes of IP-in-IP header per packet (same as basic Mobile IP)",
        "Requires two simultaneously usable links; benefit depends on the hold time (drain) of the old link."),
    ApproachInfo(
        "ro-model", "Simplified route-optimisation model",
        "SIMPLIFIED MODEL inspired by Mobile IPv6 route optimisation (RFC 6275). Route optimisation belongs to Mobile IPv6, "
        "not Mobile IPv4. This is NOT Mobile IPv6 and NOT a standards implementation (see app/mobileip/ro.py).",
        "Basic Mobile IP registration plus a Return Routability exchange (home + care-of tokens) and an authenticated Binding "
        "Update to the correspondent, which then sends directly to the care-of address (IP-in-IP stand-in for the IPv6 routing header).",
        "Binding Update accepted by the correspondent (route-optimised path active)",
        "20 bytes of IP-in-IP header per packet on the DIRECT path (Mobile IPv6 uses a 24-byte routing header)",
        "The correspondent keeps using the old care-of address until the new binding is accepted, so handoff can be slower than via the home agent."),
    ApproachInfo(
        "quic-migration", "QUIC connection migration",
        "Real QUIC engine (aioquic) in the emulator (RFC 9000 section 9 connection migration). Requires aioquic.",
        "The client app keeps ONE QUIC connection; when its local address changes it sends from the new address with a fresh "
        "connection ID; the server validates the new path (PATH_CHALLENGE/RESPONSE) and continues the same streams.",
        "first new data delivered to the mobile node after the address change (path validation is not exposed by the aioquic API)",
        "none specific to mobility",
        "Works only for applications that use QUIC. It does NOT make arbitrary TCP applications mobile."),
)}

# (status, reason) per approach and environment. status: supported | unverified | unsupported
SUPPORT = {
    "tcp-reconnect": {
        "emulated": ("supported", "TcpLite transport model + shared application logic"),
        "loopback": ("supported", "real kernel TCP; validates the application logic only, timings are not comparable (no path delay)"),
        "live": ("unsupported", "not implemented in Stage 5 (it would only need the probe apps launched in the namespaces)"),
    },
    "mip-basic": {
        "emulated": ("supported", "protocol code + byte-accurate IP-in-IP on the emulated network"),
        "loopback": ("unsupported", "needs kernel tunnels and routing, which loopback sockets do not provide"),
        "live": ("unverified", "Stage 4 live demo through the adapter; written but not run in the development sandbox (no iproute2/sudo)"),
    },
    "mip-mbb": {
        "emulated": ("supported", "link overlap + registration from the new interface in the emulator"),
        "loopback": ("unsupported", "needs kernel tunnels and routing, which loopback sockets do not provide"),
        "live": ("unverified", "live demo with overlap ordering; written but not run in the development sandbox"),
    },
    "ro-model": {
        "emulated": ("supported", "simplified RO model on the emulated network"),
        "loopback": ("unsupported", "needs routing and tunnels"),
        "live": ("unsupported", "the RO model has no kernel data plane / daemons (emulator only)"),
    },
    "quic-migration": {
        "emulated": ("unverified", "aioquic driven in virtual time; needs `pip install aioquic`; written but never executed in the development sandbox"),
        "loopback": ("unsupported", "no loopback QUIC harness implemented (the emulator drives aioquic in virtual time instead)"),
        "live": ("unsupported", "not implemented in Stage 5"),
    },
}

# approaches that can take part in make-before-break experiments
MBB_CAPABLE = {"mip-mbb"}
MBB_REASON = {
    "tcp-reconnect": "the TCP baseline does not use link overlap (it does not pre-establish the new connection), so an MBB run would just delay link release",
    "mip-basic": "basic Mobile IP is break-before-make by definition here; use approach 'mip-mbb' for the overlap variant",
    "ro-model": "the RO model is only defined for break-before-make in this stage",
    "quic-migration": "this harness does not exploit link overlap (no probing of the new path before the old one is released)",
}


# Stage 7: configurations of one approach that the experiment framework treats as separate, comparable "approach configurations".
# Everything else (approach ids, where each can run, why not) is discovered from APPROACHES / SUPPORT above, not listed again.
# (variant label, ExperimentSpec overrides)
VARIANTS = {
    "tcp-reconnect": (("notify", {"tcp_detect": "event"}), ("stall-timeout", {"tcp_detect": "timeout"})),
}


def approach_configs(approaches=None) -> list:
    """[(config_id, approach_id, variant, overrides)] for the requested approach ids (default: every registered approach)."""
    out = []
    for a in (approaches or list(APPROACHES)):
        if a not in APPROACHES:
            raise KeyError(f"unknown approach {a!r}; known: {sorted(APPROACHES)}")
        for variant, kw in VARIANTS.get(a, (("", {}),)):
            out.append((f"{a}-{variant}" if variant else a, a, variant, dict(kw)))
    return out
