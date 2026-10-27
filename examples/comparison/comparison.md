# Mobility comparison (generated)

Common conditions: topology `topology_ro.yaml`, probes 100 pps x 64 B for 10 s, moves [[4.0, 'eth-b'], [7.0, 'eth-a']], movement-detection delay 20 ms, make-before-break old-link hold 100 ms (mip-mbb only).

## Capability matrix

| Approach | Standards status | Recovery point (handoff latency ends when...) | Mobility overhead |
|---|---|---|---|
| **TCP reconnection baseline** (`tcp-reconnect`) | No mobility protocol. Application-level behaviour over TCP (RFC 9293). In the emulator TCP is the simplified TcpLite model; the same application logic is validated over real kernel TCP on loopback. | first new (non-duplicate) data frame delivered to the mobile node on the NEW connection | none specific to mobility (the peer sees a new address and a new connection) |
| **Basic Mobile IP** (`mip-basic`) | Educational Mobile IPv4-style demonstration (Stage 4). NOT RFC 5944 compliant: HMAC-SHA256 instead of keyed-MD5, no Foreign Agent, no ARP, no reverse tunnelling (see docs/MOBILE_IP.md). | first accepted registration reply (the Home Agent now tunnels to the new care-of address) | 20 bytes of IP-in-IP header per packet on the forward path (via the Home Agent) |
| **Mobile IP with make-before-break handoff** (`mip-mbb`) | Basic Mobile IP (above) with an overlap period: the new link is up before the old one is released. A testbed extension, not a separate protocol; the live demo uses a single tunnel device so in-flight packets for the old care-of address are not accepted there (the emulator accepts both addresses during the overlap). | first accepted registration reply | 20 bytes of IP-in-IP header per packet (same as basic Mobile IP) |
| **Simplified route-optimisation model** (`ro-model`) | SIMPLIFIED MODEL inspired by Mobile IPv6 route optimisation (RFC 6275). Route optimisation belongs to Mobile IPv6, not Mobile IPv4. This is NOT Mobile IPv6 and NOT a standards implementation (see app/mobileip/ro.py). | Binding Update accepted by the correspondent (route-optimised path active) | 20 bytes of IP-in-IP header per packet on the DIRECT path (Mobile IPv6 uses a 24-byte routing header) |
| **QUIC connection migration** (`quic-migration`) | Real QUIC engine (aioquic) in the emulator (RFC 9000 section 9 connection migration). Requires aioquic. | first new data delivered to the mobile node after the address change (path validation is not exposed by the aioquic API) | none specific to mobility |

| Approach | emulated (virtual time) | loopback (real sockets) | live namespaces (sudo) |
|---|---|---|---|
| `tcp-reconnect` | yes: TcpLite transport model + shared application logic | yes: real kernel TCP; validates the application logic only, timings are not comparable (no path delay) | no: not implemented in Stage 5 (it would only need the probe apps launched in the namespaces) |
| `mip-basic` | yes: protocol code + byte-accurate IP-in-IP on the emulated network | no: needs kernel tunnels and routing, which loopback sockets do not provide | yes, UNVERIFIED here: Stage 4 live demo through the adapter; written but not run in the development sandbox (no iproute2/sudo) |
| `mip-mbb` | yes: link overlap + registration from the new interface in the emulator | no: needs kernel tunnels and routing, which loopback sockets do not provide | yes, UNVERIFIED here: live demo with overlap ordering; written but not run in the development sandbox |
| `ro-model` | yes: simplified RO model on the emulated network | no: needs routing and tunnels | no: the RO model has no kernel data plane / daemons (emulator only) |
| `quic-migration` | yes, UNVERIFIED here: aioquic driven in virtual time; needs `pip install aioquic`; written but never executed in the development sandbox | no: no loopback QUIC harness implemented (the emulator drives aioquic in virtual time instead) | no: not implemented in Stage 5 |

## Measured results (emulated environment, deterministic virtual time)

| Approach (variant) | Status | Handoff latency mean/max (ms) | Service gap mean/max (ms) | Packets lost (total) | RTT before→after, 1st handoff (ms) | Signalling per handoff (msgs / bytes) | Mobility overhead (B/pkt) | Transport connection preserved | Endpoint address preserved |
|---|---|---|---|---|---|---|---|---|---|
| `tcp-reconnect` (notify) | ok | 100.0 / 128.0 | 105.0 / 135.0 | 0 | 26.0→54.0 | 4.0 / 138 | 0.0 | no | no |
| `tcp-reconnect` (stall-timeout) | ok | 580.0 / 608.0 | 585.0 / 615.0 | 0 | 26.0→54.0 | 4.0 / 138 | 0.0 | no | no |
| `mip-basic` | ok | 71.0 / 91.0 | 90.0 / 110.0 | 16 | 33.0→67.0 | 2.0 / 120 | 20.0 | n/a (connectionless) | yes |
| `mip-mbb` | ok | 71.0 / 91.0 | 20.0 / 30.0 | 0 | 33.0→67.0 | 2.0 / 120 | 20.0 | n/a (connectionless) | yes |
| `ro-model` | ok | 151.0 / 199.0 | 165.0 / 214.0 | 31 | 26.0→54.0 | 6.0 / 202 | 20.0 | n/a (connectionless) | yes |
| `quic-migration` | **unavailable** | — | — | — | — | — | — | — | — |

## Unsupported or unavailable scenarios (reported, not skipped)

| Scenario | Status | Reason |
|---|---|---|
| `quic-migration` / emulated | unavailable | the aioquic library is not installed (pip install aioquic); the QUIC adapter cannot run |
| `tcp-reconnect` / emulated / make-before-break | unsupported | tcp-reconnect with make-before-break: the TCP baseline does not use link overlap (it does not pre-establish the new connection), so an MBB run would just delay link release |
| `mip-basic` / emulated / make-before-break | unsupported | mip-basic with make-before-break: basic Mobile IP is break-before-make by definition here; use approach 'mip-mbb' for the overlap variant |
| `ro-model` / emulated / make-before-break | unsupported | ro-model with make-before-break: the RO model is only defined for break-before-make in this stage |
| `quic-migration` / emulated / make-before-break | unsupported | quic-migration with make-before-break: this harness does not exploit link overlap (no probing of the new path before the old one is released) |
| `mip-basic` / loopback | unsupported | mip-basic in the loopback (real sockets) environment: needs kernel tunnels and routing, which loopback sockets do not provide |
| `mip-mbb` / loopback | unsupported | mip-mbb in the loopback (real sockets) environment: needs kernel tunnels and routing, which loopback sockets do not provide |
| `ro-model` / loopback | unsupported | ro-model in the loopback (real sockets) environment: needs routing and tunnels |
| `quic-migration` / loopback | unsupported | quic-migration in the loopback (real sockets) environment: no loopback QUIC harness implemented (the emulator drives aioquic in virtual time instead) |
| `ro-model` / live | unsupported | ro-model in the live namespaces (sudo) environment: the RO model has no kernel data plane / daemons (emulator only) |
| `tcp-reconnect` / live | unsupported | tcp-reconnect in the live namespaces (sudo) environment: not implemented in Stage 5 (it would only need the probe apps launched in the namespaces) |
| `quic-migration` / live | unsupported | quic-migration in the live namespaces (sudo) environment: not implemented in Stage 5 |
