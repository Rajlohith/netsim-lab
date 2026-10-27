# Basic Mobile IP (Stage 4) — an educational Mobile IPv4-style demonstration

Code: `backend/app/mobileip/`. **This is not an RFC 5944 (Mobile IPv4) compliant implementation and makes no
such claim.** It reproduces the core ideas — home address, care-of address, registration with a lifetime, a Home
Agent binding table, IP-in-IP tunnelling — so they can be observed and measured. Read the status table first.

## 1. What is implemented, simplified, or absent

| Area | Status | Notes |
|---|---|---|
| Mobile Node, Home Agent, home address, care-of address | **Implemented** (protocol logic, `home_agent.py`, `mobile_node.py`) | co-located care-of address mode only (the MN decapsulates itself) |
| Registration Request / Reply wire format | **Implemented, with deviations** (`messages.py`) | RFC 5944 fixed-part layout, flags, codes 0/1/128-136 subset, Mobile-Home Authentication extension layout. **Deviations:** HMAC-SHA256 instead of the mandatory keyed-MD5; UDP port 4340 instead of 434 (unprivileged tools); no other extensions |
| Binding lifetime, HA lifetime shortening, renewal | **Implemented** | HA caps the lifetime (default 300 s); MN renews at 50 % of the granted lifetime; expiry runs on a monotonic clock |
| Deregistration | **Implemented** | lifetime 0 with care-of address = home address |
| Retransmission | **Implemented, own timers** | 1 s, 2 s, 4 s, 8 s cap, 3 retransmissions, new identification each time; late replies to earlier attempts are still accepted |
| Replay protection | **Implemented** (timestamp method) | NTP-format identification, ±7 s window, strictly increasing; code 133 with the HA's time lets a skewed MN resynchronise |
| Binding management | **Implemented** | one binding per home address (S bit is answered with code 1) |
| IP-in-IP (RFC 2003 style) encapsulation | **Implemented in Python** (`ipip.py`), **kernel `ipip` devices on Linux** | outer header protocol 4, +20 bytes, inner TTL decremented, DF/TOS copied, checksum/fragment validation. No ICMP errors, no PMTU handling, no tunnel-loop detection |
| Delivery to the MN through its Home Agent | **Demonstrated in the emulator; implemented with kernel tunnels in the live demo (live run not verified in the dev sandbox)** | CN → HA → tunnel → MN, replies direct to the CN (triangle routing) |
| Movement between two foreign networks | **Emulated** | an interface of the MN is brought up/down; the MN is told the new care-of address after a modelled detection delay (20 ms by default) |
| Foreign Agents, agent advertisement/solicitation, movement detection | **Absent** | |
| Home network | **Emulated** | `10.10.100.0/24` is virtual: addresses live on `lo`; there is no ARP, proxy/gratuitous ARP, or home link |
| Reverse tunnelling (RFC 3024), GRE/minimal encapsulation, multicast/broadcast forwarding, simultaneous bindings | **Absent**. Requests setting the T, G or M flags get code 128; the B flag is ignored; the S flag is answered with code 1 ("simultaneous bindings unsupported") | |
| HA discovery, dynamic home address assignment, AAA, NAI | **Absent** | |

## 2. Architecture

```
 cn (10.10.1.2) ---- ha ----+---- ana ---- mn  (eth-a 10.10.4.2)         ha also owns 10.10.100.1 (agent address, lo)
                            \---- anb ---- mn  (eth-b 10.10.5.2)         mn owns   10.10.100.2 (home address, lo)
 probes:  cn --> 10.10.100.2   ha: route 10.10.100.2/32 -> tunnel to the current care-of address
```
- **Protocol cores** (`home_agent.py`, `mobile_node.py`, `messages.py`, `binding.py`) are pure logic: they take bytes and a clock and
  return bytes; they never touch sockets or the kernel. The same code runs in all three environments below.
- **Data-plane interface** (`dataplane.py`): the HA asks for `install_binding / remove_binding`, the MN for `set_care_of / clear`.
- **Environments**
  1. `mobility_sim.py` + `simnet.py` — discrete-event emulator, virtual time, byte-accurate IPv4/UDP/IP-in-IP, same addresses/delays as
     `config/topology.yaml`. Deterministic: two runs produce identical files. **Needs no root and no network.**
  2. `runtime.py` — real UDP sockets (`HomeAgentServer`, `MobileNodeClient`, CLI `python -m app.mobileip.runtime ha|mn`).
  3. `demo.py` — the live demo inside the Stage 2 namespaces with kernel `ipip` tunnels.
- **Privilege split (live demo).** The daemons run *inside* the namespaces as **your** user (via `setpriv`). They cannot run `ip`, so
  they ask the orchestrator through a file handshake (`dataplane.py`: `Handshake*DataPlane` ↔ `DataPlaneController`), which applies
  allow-listed commands with `sudo -n` per command (`applier.py`) and acknowledges. The HA replies only **after** the tunnel is
  installed, so registration delay in the live demo includes this hand-off (a few ms).
- **Containment.** Tunnel endpoints may only be addresses the topology config assigns to the mobile node (`applier.py`); every
  command is `ip -n nsl-<node> ...` (`netem/safety.py` rejects host-namespace tunnels, non-`tnl-` devices, non-/32 routes, firewall
  tools). Cleanup deletes the tunnels, removes the two `/32` addresses and brings all MN interfaces back up, also after failures.

## 3. Protocol behaviour in brief
1. Attach → MN configures decapsulation for the new CoA, then sends a signed Registration Request (HA address, home address, CoA,
   lifetime, fresh identification) from the CoA to the HA.
2. HA: decode → right HA address → known MN and valid MAC (else 131) → identification fresh and newer (else 133, reply carries HA time)
   → supported flags (D required; M/G/T rejected with 128) → CoA sane (not in the home network, unicast; else 134) → install data plane
   (failure → 130) → binding created/updated with the granted lifetime → signed reply (code 0, or 1 if S was set).
3. MN accepts only authenticated replies that match an outstanding identification; anything else is logged and ignored. It renews at
   half the granted lifetime. On moving it simply attaches again; the HA's binding update changes where the home address is reachable.
4. HA tick expires bindings (removing the tunnel); `shutdown()` removes everything.
5. Stale packets: after a handoff, packets already tunnelled to the **old** care-of address find the old link dead. What happens
   next depends on *how* the link went away, and this was checked on a **real Linux kernel** (`tests/test_live_kernel_lab.py`):
   * If only the **mobile node's** end is taken down (what the live demo does with `ip link set eth-a down` in the mobile node's
     namespace), the access router's end of the veth keeps its address and connected route (admin-up, no carrier). The packets are
     **not** looped: the router tries ARP and, after about 3 s, answers with ICMP *host unreachable*.
   * Only if **both ends** go down does the router lose its connected route; its default route then points back at the Home Agent and
     the packets bounce HA <-> router until the TTL expires (ICMP *time exceeded*).
   The emulator reproduces both: `link_semantics="both-ends"` (default; the loop, counted as `ha:ttl_expired`) and `"kernel-veth"`
   (the router keeps the route, packets die at the dead link, counted as `<router>:link_down`). Delivery metrics (loss, gaps, latency)
   are identical in both modes. (An earlier version of this document claimed the live testbed "behaves the same way" as the default
   emulator mode; that was wrong for the demo as implemented.)

## 4. Metrics (`python -m app.mobileip analyze RUN_DIR`)
| Metric | Definition | Where it comes from |
|---|---|---|
| Registration delay | MN request sent → matching authenticated reply received | MN's own monotonic clock (`mip_events*.csv`) |
| Time to registered | attach → first accepted reply (includes retransmits and clock resync) | same |
| Handoff latency | link change → first accepted reply = detection + registration | link_change event + MN events (same-host clock) |
| Connectivity interruption | arrival gap / loss burst around the handoff | Stage 3 probe metrics (`metrics.json`) |
| Tunnel overhead | emulator: counted from real encapsulated bytes (20 B/packet = 21.7 % of a 92-byte inner packet). **Live: analytic** (the kernel does the tunnelling; only the constant RFC 2003 header is known) | `tunnel_stats` event / `mip_summary.json` |
| Failures | auth failures, replays, rejections, timeouts, expiries, data-plane errors, drops by reason | event kinds + `mip_summary.json` |
Unavailable values are `null` with a warning; nothing is interpolated (see `docs/TRAFFIC_MEASUREMENT.md` for the probe side).
Emulator timings are *model* values (e.g. 31 ms = 2 × 15 ms path + 1 ms HA processing); they validate the code, not the Linux kernel.

## 5. Security, authentication and replay: limitations
- **Authentication** is a static shared key per MN (HMAC-SHA256 over everything before the authenticator, SPI ≥ 256). No key
  distribution, rotation, or per-session keys. The demo key is random per run and stored in `<run>/mip.key` (mode 0600); it is never put
  on a command line. Examples in `examples/` use a fixed, public demo key — **never reuse it**.
- **No confidentiality or integrity for user data**: IP-in-IP carries plain packets. Anyone able to inject packets with the right outer
  addresses can inject inner packets at the MN (the kernel tunnel only checks the outer addresses). IPsec is not used.
- **Replay protection** needs roughly synchronised clocks (±7 s). A replayed or reordered request older than the last accepted one is
  rejected, but an attacker who *suppresses* a newer request can still delay a valid older one within the window. The 133 reply
  discloses the HA's time to authenticated peers only (it is signed), but nothing stops request flooding: the HA verifies an HMAC for
  every datagram and has no rate limiting, cookie, or blacklist → CPU denial of service is possible.
- **The HA does not verify that the MN owns the care-of address.** A holder of the key can redirect its traffic to any address
  (redirection/reflection). The live applier restricts CoAs to testbed addresses; the HA core itself does not.
- **Triangle routing** sends replies with the home address as source directly from the foreign network. Real networks with ingress
  filtering would drop them; reverse tunnelling is not implemented. The lab disables `rp_filter` in its namespaces.
- Control messages are unencrypted (contents visible to anyone on path), the UDP checksum is the only transport integrity, and
  malformed/unauthenticated requests are answered with codes 131/134 (an oracle for key-less probing of valid home addresses).
- Failure handling worth knowing: a 131/unsigned rejection cannot be authenticated by the MN, so the MN **ignores** it and ends in a
  timeout rather than trusting an unauthenticated "no".

## 6. Running it
**Emulator (no root):**
```bash
make test                                              # includes all Mobile IP tests
PYTHONPATH=backend python -m app.mobileip sim --out-dir data/results --duration 10
PYTHONPATH=backend python -m app.mobileip analyze examples/mobileip/mip-handoff/example-1
python scripts/generate_mobileip_examples.py           # regenerate examples/mobileip/
```
**Live namespace demo (Linux, sudo; unverified in the dev sandbox):**
```bash
sudo modprobe ipip                       # one-time; Fedora: kernel-modules-extra may be required
scripts/topology.sh create               # Stage 2 lab
PYTHONPATH=backend python -m app.mobileip demo             # dry-run: prints the privileged commands
sudo -v && PYTHONPATH=backend python -m app.mobileip demo --execute
scripts/topology.sh status; scripts/topology.sh destroy    # inspect / reset the lab
```
The demo registers on network A, starts probes CN → home address (50 pps), moves to B at +8 s and back to A at +14 s, deregisters, and
writes `data/results/mip-live-demo/<run-id>/` (raw files + `mip_metrics.json` + `metrics.json`). Inspect while it runs with
`sudo ip -n nsl-ha tunnel show` / `sudo ip -n nsl-ha route`. Live test: `sudo -v && NETSIM_RUN_PRIVILEGED=1 python -m unittest tests.test_mip_live`.

## 6b. Make-before-break (Stage 5 extension)
`MobilitySpec(make_before_break=True, old_link_hold_ms=...)` (emulator) and `DemoSpec(make_before_break=True)` (live) keep the old link up,
register from the NEW care-of address and release the old link `hold` ms after the registration succeeded. In the emulator the mobile node
decapsulates for both care-of addresses during the overlap (zero loss with a 100 ms drain, see `docs/MOBILITY_COMPARISON.md`); in the live
demo one tunnel device is re-pointed, so packets still in flight for the old care-of address are not accepted there.
Bug fixed in this stage: `handoff_end` was recorded about 1 ms after the movement instead of when registration completed.

## 7. Verification status
Exact counts: `docs/TEST_REPORT.md` (generated). Verified by automated tests in a sandbox **without** iproute2/sudo and whose kernel has
**no ipip/gre/dummy support**: wire formats, authentication, replay, lifetime/expiry, retransmission, binding updates, IP-in-IP bytes,
the emulated end-to-end handoff (delivery, interruption, overhead, failures, cleanup), registration over real UDP sockets, the allow-list
and the handshake/controller logic, the demo orchestrator against a fake host, **and, on the real kernel, the Stage 2 lab the demo builds
on** (namespaces, veth, routing, forwarding, failover, cleanup) plus our IPv4/UDP/TCP/IP-in-IP packet encoding against the kernel's parser.
**Not verified:** kernel `ipip` tunnels (impossible on this kernel), `ip` command syntax, the `setpriv` launch, and the daemons running in
namespaces (only `tests/test_mip_live.py`, which must be run on a Linux host with ipip support).
