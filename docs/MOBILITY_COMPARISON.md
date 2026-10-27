# Mobility comparison (Stage 5)

Five approaches behind one experiment interface, measured with the same probes, the same movement and the same metrics.
Code: `backend/app/compare/` (interface, adapters, normaliser, matrix) plus `app/mobileip/ro*.py` (route optimisation model).
Generated results: `examples/comparison/` (`comparison.md` / `comparison.json` and one run directory per scenario).

## 1. What each approach is — and is not
| Approach | What it is | What it is **not** |
|---|---|---|
| `tcp-reconnect` TCP reconnection baseline | No mobility protocol. The address change kills the TCP connection; the application detects it, reconnects from the new address and resumes with its own sequence numbers. | Not "TCP that survives mobility". An unmodified TCP application just loses its connection. In the emulator TCP is the **TcpLite model** (`compare/tcplite.py`), not the Linux stack; the same application logic is validated over **real kernel TCP** on loopback. |
| `mip-basic` Basic Mobile IP | Stage 4 educational Mobile IPv4-style demo (registration, bindings, IP-in-IP via the Home Agent, triangle routing). | **Not RFC 5944 compliant** (see `docs/MOBILE_IP.md`). |
| `mip-mbb` Mobile IP, make-before-break | Basic Mobile IP where the new link is brought up and registered over **before** the old one is released (`old_link_hold_ms` later, so in-flight packets drain). | Not a different protocol: a testbed/handoff-procedure extension. In the live demo a single tunnel device means packets still in flight for the old care-of address are not decapsulated, so the full benefit is only shown in the emulator. |
| `ro-model` Simplified route optimisation | A **simplified model** of the Mobile IPv6 route-optimisation idea (Return Routability tokens → authenticated Binding Update → correspondent sends directly to the care-of address). | **Route optimisation belongs to Mobile IPv6 (RFC 6275), not Mobile IPv4.** This is not Mobile IPv6 and not a standards implementation: IPv4 transport, own message layout, IP-in-IP instead of the routing header (20 B vs 24 B), one nonce index, stateless nonces, no IPsec, no fallback to the home path during handoff. Details at the top of `app/mobileip/ro.py`. |
| `quic-migration` QUIC connection migration | The real **aioquic** engine driven in virtual time: one QUIC connection, client moves to a new source address with a fresh connection ID, server validates the path. | **Applies only to applications that use QUIC.** It does not make arbitrary TCP applications mobile. **Not executed in the development sandbox** (aioquic could not be installed there). |

Chosen RO model, in one paragraph: after registering with the Home Agent the mobile node runs *Return Routability* with the
correspondent (a **home token** that reaches it through the Home Agent proves the home address; a **care-of token** sent to
the new address proves the care-of address), derives `Kbm = SHA256(home_token | coa_token)` and sends a Binding Update
authenticated with `Kbm`. The correspondent keeps a binding cache entry with a lifetime and sequence number and sends
IP-in-IP packets straight to the care-of address; when the entry expires it falls back to the home address (via the Home Agent).
The home token is reused for 210 s, so a handoff costs one care-of test + one Binding Update (2 direct round trips) after the
Home Agent registration. Until the new Binding Update is accepted the correspondent still uses the old care-of address
(loss) — that is the weakness the model deliberately reproduces.

## 2. The common experiment interface
```bash
PYTHONPATH=backend python -m app.compare list                         # approaches, environments, availability here
PYTHONPATH=backend python -m app.compare run --approach mip-mbb       # one experiment (also: --handoff-mode, --tcp-detect, --hold-ms)
PYTHONPATH=backend python -m app.compare matrix --out-dir data/results/comparison
```
```python
from app.compare.experiment import ExperimentSpec
from app.compare.runner import run_experiment
result = run_experiment(ExperimentSpec("ro-model", duration_s=10), "data/results")   # status, reason, metrics, run_dir
```
`ExperimentSpec` carries approach, environment (`emulated` | `loopback` | `live`), probe workload, the planned movements,
`handoff_mode` (`bbm` | `mbb`), the movement-detection delay (same for every approach) and approach parameters.
`run_experiment` always writes `experiment.json` to the run directory with `status` ∈ `ok | unsupported | unavailable | failed`
and the reason. **Unsupported scenarios are executed as refusals, recorded, and listed in the matrix** (e.g. TCP + make-before-break,
route optimisation on the live testbed, QUIC without aioquic) instead of being skipped.

Every adapter produces the same raw files: the Stage 3 probe files (`sent/received/echoes/events.csv`, metas, `scenario.json`),
`approach_events.csv` (common event kinds: `link_change, attach, signal_tx, signal_rx, recovered, conn_opened, conn_closed,
retransmit, failure, note`) and `experiment_summary.json`; one normaliser (`normalize.py`) turns them into
`comparison_metrics.json`. To add an approach: write an adapter that emits those files, register it in `approaches.py`, add its
independent validation tests, and the matrix picks it up.

## 3. Common conditions and core metrics
Topology `config/topology_ro.yaml` (Stage 2 lab + direct links cn–ana / cn–anb), probes CN → MN at 100 pps × 64 B for 10 s (the
transport approaches carry them over one reliable stream with the same echo), moves A→B at 4 s and B→A at 7 s, movement
detected 20 ms after the link change for every approach.

| Metric | Definition (all from raw files; `null` when unavailable) |
|---|---|
| Handoff latency | link change → the approach's **recovery point** (below) |
| Service gap | longest inter-arrival gap at the mobile node around the handoff — the comparable "interruption" for reliable and unreliable carriers |
| Packets lost | probes never delivered (sequence numbers); reliable carriers resend, so this is ~0 and the cost shows up as gap/delay |
| RTT before → after | median echo RTT before/after the handoff (includes path change) |
| Signalling | mobility/transport messages and bytes the mobile node sent/received between link change and recovery |
| Mobility overhead | bytes added per packet *because of* the mobility mechanism (20 B IP-in-IP for Mobile IP/RO; none for TCP/QUIC) |
| Continuity flags | transport connection preserved (n/a for connectionless UDP), endpoint address preserved, all handoffs recovered, data-loss free |

Recovery points: TCP — first new data on the *new* connection; Mobile IP / MBB — first accepted registration reply; RO — Binding
Update accepted by the correspondent; QUIC — first new data after the address change (aioquic does not expose path validation).
These are *protocol-defined* points, so latency is not strictly comparable across approaches; the service gap is.

## 4. Results (emulated, deterministic virtual time; see `examples/comparison/comparison.md` for the full generated report)
| Approach (variant) | Handoff latency mean/max (ms) | Service gap mean/max (ms) | Lost | RTT before→after (ms) | Signalling /handoff | Overhead B/pkt | Transport conn. preserved |
|---|---|---|---|---|---|---|---|
| TCP reconnect (OS notification) | 100 / 128 | 105 / 135 | 0 | 26→54 | 4 msgs / 138 B | 0 | no |
| TCP reconnect (500 ms stall timeout) | 580 / 608 | 585 / 615 | 0 | 26→54 | 4 / 138 | 0 | no |
| Basic Mobile IP | 71 / 91 | 90 / 110 | 16 | 33→67 | 2 / 120 | 20 | n/a |
| Mobile IP make-before-break (100 ms drain) | 71 / 91 | 20 / 30 | 0 | 33→67 | 2 / 120 | 20 | n/a |
| Route-optimisation model | 151 / 199 | 165 / 214 | 31 | 26→54 | 6 / 202 | 20 | n/a |
| QUIC migration | **not run: aioquic unavailable here** | | | | | | |

How to read this honestly:
* These are **model results** on one topology, not measurements of real networks or kernels. They show *mechanisms*: reconnection costs
  detection + ≈2 RTT; Mobile IP costs detection + a registration round trip *to the Home Agent* and loses what is in flight; overlap
  (MBB) removes most of that if the old link is kept long enough; RO shortens the steady-state path (26 vs 33 ms) but is *worse* at the
  handoff because the correspondent keeps using the stale care-of address until the Binding Update (+2 RTT) completes.
* The RO advantage in steady state exists only because this comparison topology gives the correspondent direct links to the access
  networks; with the correspondent behind the Home Agent the paths would be equal.
* TCP shows zero loss because the application buffers and resumes; its price is the gap and the new connection. Loss is therefore
  not comparable between reliable and unreliable carriers — use the service gap.
* Mobile IP/RO keep the home address (peers see no change); TCP/QUIC expose a new address.
* Overhead: 20 B/packet is 21.7 % of these small 92-byte packets and ~1.3 % of a 1500-byte one.
* Parameters matter: the TCP stall timeout, MBB hold time, registration lifetime, and movement-detection delay change the numbers.

## 5. How each approach was validated before being compared
Exact, generated pass/fail/skip counts per module are in **`docs/TEST_REPORT.md`** (written by `scripts/run_all_tests.py` from an actual run;
nothing below is hand-counted).
| Approach | Independent validation |
|---|---|
| TcpLite model | `test_compare_tcplite`: handshake timing, RFC 6298 RTO estimate and back-off schedule (0.2/0.6/1.4 s), SYN give-up at 31 s, timeout at 20.6 s, RST handling, in-order recovery of a lost segment, checksum rejection, refusal to send from a vanished address |
| TCP application logic | `test_compare_apps`: framing, resume/replace-stale-connection, failure on unsupported reconnect, duplicates; **over real kernel TCP**: 200 probes, 0 lost, 0 duplicated, 3 connections |
| TCP baseline in the emulator | `test_compare_emulated`: contiguous in-order delivery, exact latency, stall-timeout policy, **expected failure** (server stops accepting → no recovery), repeatability |
| Basic Mobile IP | Stage 4 suites (`test_mip_*`): protocol, real UDP on loopback, emulator, data plane, demo against a fake host |
| MBB | `test_compare_mbb`: zero loss with drain, losses without drain, release exactly `hold` after registration, registration from the new interface, refusal by other approaches, **rapid back-and-forth regression** |
| RO model | `test_compare_ro`: tamper / redirection attack / replay / expired nonce / forged token & ack, token reuse, expiry fallback, graceful failure without RO support |
| QUIC | `test_compare_quic`: **skipped here** (needs the real aioquic, never run). `test_compare_quic_glue`: our adapter code against a clearly labelled **stub** (`tests/stubs/`), which found and fixed a timer re-arm hang; it says nothing about aioquic |
| Common interface | `test_compare_interface`, `test_compare_validation`, `test_compare_semantics`: same files and metric structure for every approach, explicit reporting of unsupported/unavailable/invalid, workload and movement parity across approaches, provenance |
| Real Linux kernel | `test_live_kernel_lab`: the project's own Stage 2 command plans executed on the real kernel (veth, routing, forwarding, cleanup, failover, stale-address behaviour); `test_kernel_wire_formats`: our IPv4/UDP/TCP/IP-in-IP bytes accepted by the real stack |

## 6. Verification status and limits
* **Verified here:** everything above except QUIC and the live namespace paths.
* **Written but unverified here:** the QUIC adapter (`quic_sim.py`, uses aioquic's sans-IO API as documented; expect to adjust on first run),
  the live Mobile IP / MBB adapters (need iproute2, sudo, `ipip`).
* **Not implemented (reported as unsupported):** live TCP and QUIC runs, loopback QUIC, route optimisation on the live testbed.
* Emulated timings depend on the model (no queues, no bandwidth, TcpLite without congestion control). Loopback runs have
  `timing_comparable: false`.

## 7. Verification audit (Stage 5 review)
**Classification** (details and evidence in `PROJECT_STATUS.md`):
| Approach | Classification |
|---|---|
| TCP reconnection | **Partially implemented**: application logic implemented and verified on real kernel TCP (loopback); the compared timings come from the simplified TcpLite *model*; no live-namespace TCP run |
| Basic Mobile IP | **Partially implemented**: protocol logic implemented and tested (incl. real UDP sockets); the data path is **simulated** (byte-accurate IP-in-IP in an emulator); kernel tunnels **untested** (this kernel has no ipip) |
| Route optimisation | **Simulated** simplified model (not Mobile IPv6), emulator only |
| Mobile IP make-before-break | **Simulated** (emulator); live ordering tested against a fake host only |
| QUIC migration | **Untested** against the real library; adapter written, glue exercised on a stub only |

**What is a measurement, what is simulated, what is analytic** (every `comparison_metrics.json` carries a `provenance` block):
| Value | Origin |
|---|---|
| All numbers in section 4 and `examples/` | **Simulated** (deterministic virtual time; no real delays, queues or kernel stacks) |
| Real-kernel UDP probe failover (below) | **Measured** on a real kernel (veth + routing, no netem, no mobility protocol) |
| TCP loopback runs | **Measured** on real kernel TCP, but with no path delay (`timing_comparable: false`) |
| Tunnel overhead, emulator | **Counted** from real encapsulated bytes in the emulator |
| Tunnel overhead, live demo | **Analytic** (20 B; the kernel does the tunnelling) |
| Registration delay, handoff latency (all) | **Simulated** (model path delays + 1 ms modelled HA processing) |

Real-kernel measurement (3 runs, 600 probes at 200 pps each, MN -> CN across the real veth lab, `eth-a` taken down at +1 s, default
route fails over to `eth-b` by itself): **0 packets lost, maximum inter-arrival gap 5.9 - 7.0 ms, median RTT 0.10 - 0.14 ms**. This is a
plain routing failover; it contains no registration, no tunnel, no netem delay, so it must not be compared with the Mobile IP numbers.

**Defects found by this audit and fixed (each with a regression test):**
1. Make-before-break: a stale "release old link" timer could take down the link the node had just moved back to (381 of 550 probes lost).
2. Stage 4: `handoff_end` was recorded 1 ms after the move instead of at registration completion.
3. TcpLite let a connection send from an address the node no longer had.
4. The experiment interface reported `ok` for meaningless input (`duration 0`, negative rate, payload that overflows the MTU once tunnelled => 100 % loss, unknown interfaces, moves outside the run).
5. QUIC adapter timer could be re-armed at the same virtual instant forever (hang); the simulator now fails fast on an event storm.
6. Documentation claimed the live testbed loops stale packets like the emulator; the real kernel shows that only happens when both link ends are down (corrected; both behaviours are now modelled).
