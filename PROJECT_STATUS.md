# PROJECT_STATUS — mobile-ip-handoff (netsim-lab), state after Stage 7 (experiment and analysis framework)

Generated from an actual run: <!-- BEGIN:stamp -->**2026-10-29 01:40:21Z**, git commit `None`, Python 3.12.3, Linux-6.18.44-fc-v185-x86_64-with-glibc2.39.<!-- END:stamp -->
Stage 7 (replicated experiments, statistics, exports, charts; `backend/app/compare/{campaign,analysis,stats,plots,reproduce}.py`, `docs/EXPERIMENTS.md`) is implemented, its tests ran (section 5) and a 372-run example campaign with analysis ships in `examples/experiments/stage7/`. Stage 6 (handoff-degradation prediction, `backend/app/ml/`, `docs/HANDOFF_PREDICTION.md`) is implemented and its tests ran. The dashboard has **not** been started. Nothing here is marked complete unless it was exercised; "written but never run" is said explicitly.

## 1. Headline
* Stages 1–5 are built: project foundation, Linux network-namespace lab, UDP traffic/metrics tools, an educational Mobile IP, and a
  five-approach comparison framework.
<!-- BEGIN:headline -->* **Tests: 450 ran -> 438 passed, 0 failed, 12 skipped; 2 modules not runnable** (`test_api`, `test_config`: FastAPI / pydantic / pytest are not installed in the sandbox, so the Stage 1 API and settings tests have **never been executed**).<!-- END:headline -->
* **Could not be verified in this environment** (no network, no iproute2/`tc`/`ping`/`sudo`; kernel lacks `ipip`/`gre`/`dummy`, no visible `netem`):
  real IP-in-IP tunnels, `tc netem` delay/loss/rate, the `ip`/`tc` command-line syntax, the `setpriv` launch of daemons, the QUIC adapter against the
  real `aioquic`, the FastAPI app, the frontend (`npm install` impossible), and everything under `NETSIM_RUN_PRIVILEGED=1`.
* **Verified on a real Linux kernel** (new in this review, root + netlink, no iproute2 needed): the project's own Stage 2 command plans build
  the 5-namespace veth lab; addresses, routes with metrics, forwarding flags, reachability, forwarding-off failure, route failover, cleanup,
  rollback and host-namespace isolation all behave as designed; our hand-built IPv4/UDP/TCP/IP-in-IP packets are accepted by the real stack.

## 2. Architecture
```
spec (ExperimentSpec) -> adapter -> raw files -> normaliser -> comparison_metrics.json -> matrix (comparison.md/json)
```
| Path | Purpose | ~Lines |
|---|---|---|
| `backend/app/main.py, config.py, api/` | FastAPI skeleton (`/api/health`), settings (Stage 1) — **never executed here** | small |
| `backend/app/netem/` | Stage 2: YAML topology, command plans, **allow-list** (`safety.py`), only subprocess user (`runner.py`), `TopologyManager` | 1102 |
| `backend/app/traffic/` | Stage 3: UDP probe sender/receiver, raw CSV/JSON schema, metrics, handoff scenario | 1557 |
| `backend/app/mobileip/` | Stage 4/5: protocol cores, IP-in-IP, discrete-event emulator (`simnet.py`), live demo, route-optimisation model | 2758 |
| `backend/app/compare/` | Stage 5: common interface, approach registry, TcpLite model, shared app workload, QUIC adapter, normaliser, matrix, CLI | 1897 |
| `backend/tests/` | unit/integration/live-kernel tests, `kernel_lab.py` (netlink executor), `stubs/` (labelled aioquic stub) | 4843 |
| `config/` | `topology.yaml` (Stage 2 lab), `topology_ro.yaml` (+ direct cn–ana/cn–anb links, used for comparisons), `scenarios/` | |
| `schemas/` | raw-data schemas (CSV columns, run metadata, metrics) | |
| `scripts/` | `setup.sh`, `check_env.sh`, `topology.sh`, `run_all_tests.py`, example generators | 331 |
| `examples/` | real raw results: `results/` (loopback), `mobileip/` (emulated), `comparison/` (full matrix + one run dir per scenario) | |
| `docs/` | `ARCHITECTURE.md`, `PRIVILEGED_OPERATIONS.md`, `TRAFFIC_MEASUREMENT.md`, `MOBILE_IP.md`, `MOBILITY_COMPARISON.md`, `TEST_REPORT.md` (generated) | |
| `frontend/` | React/Vite scaffold (one page, one test) — **never installed or run here** | tiny |

Three environments exist for experiments: **emulated** (deterministic virtual-time emulator; all comparison numbers come from here),
**loopback** (real kernel sockets, no path delay), **live** (real namespaces + tunnels; written, unverified).

## 3. Stage status
| Stage | State | Verified how |
|---|---|---|
| 1 Foundation | files exist; **only structure and scientific-stack tests ran** | API/settings tests not runnable (fastapi/pydantic/pytest missing) |
| 2 Network emulation | plans/allow-list/manager **verified**; live `ip`/`tc` path **unverified** | unit tests with a fake host **and the real kernel** (veth/routing/forwarding/cleanup); no `tc netem` (kernel/tool missing) |
| 3 Traffic & metrics | **verified** | real UDP sockets on loopback, SIGTERM/SIGKILL tests, **real kernel namespaces** |
| 4 Basic Mobile IP | protocol **verified**; data plane **simulated**; kernel tunnels **untested** | unit tests, real UDP on loopback, emulator |
| 5 Comparison | 4 of 5 approaches **verified in the emulator**; QUIC **untested** | see section 4 |
| 7 Experiments & analysis | **implemented and run end to end**: 372-run emulated campaign (12 replicates/cell), statistics, CSV/JSON/Markdown exports, charts, byte-for-byte reproduction check; QUIC and live environments **not run** | `tests/test_exp_*.py` (statistics checked against known values/coverage simulations; fault-injected failed/interrupted/crashed runs; regeneration from saved results), `docs/EXPERIMENTS.md` §10 |
| 6 Handoff prediction | pipeline **implemented and run end to end on simulated data**; models trained and evaluated on held-out scenario families (shipped in `examples/ml/stage6/`); **no predictive skill beyond generator regularities** (see `docs/HANDOFF_PREDICTION.md` section 7); never run on real-network data | 63 new unit/integration tests (real emulator runs), reproducibility check (rerun from the shipped dataset gives identical results.json except timestamp) |

## 4. The five approaches
| Approach | Classification | Evidence | Not verified / limits |
|---|---|---|---|
| TCP reconnection baseline | **Partially implemented** | application logic over **real kernel TCP** (loopback): 200/200 delivered, 0 dup, in order; emulator runs on the **TcpLite model**; expected-failure and validation tests | TcpLite is a model (no congestion control/SACK/fast-retransmit); no live-namespace TCP run; timings are simulated |
| Basic Mobile IP | **Partially implemented** | registration, auth, replay, lifetime, binding update, deregistration over **real UDP**; byte-accurate IP-in-IP; emulated handoff metrics | delivery path is **simulated**; kernel `ipip` tunnels, `setpriv` launch, `ip` syntax **untested**; not RFC 5944 compliant (documented) |
| Simplified route optimisation | **Simulated** | model-level security tests (tamper, redirection attack, replay, nonce expiry, forged replies), emulator runs | a simplified model of a **Mobile IPv6** feature, not Mobile IPv6; emulator only; no live implementation |
| Mobile IP, make-before-break | **Simulated** | emulator: 0 loss with drain, losses without, exact release timing, rapid back-and-forth regression; live ordering against a fake host | live demo uses one tunnel device (in-flight packets for the old care-of address are not accepted there); live **untested** |
| QUIC connection migration | **Untested** | adapter written against aioquic's sans-IO API; glue logic exercised on a **stub** (`tests/stubs`, labelled) | **never run against real aioquic** (not installable here); expect fixes on first run; applies only to QUIC applications |

## 5. Test results (generated)
<!-- BEGIN:tests -->Totals: **438 passed / 0 failed / 12 skipped / 2 not runnable** -- per-module table below, full detail in `docs/TEST_REPORT.md` and `docs/test_results.json`.

| Module | Status | Ran | Failed | Skipped |
|---|---|---|---|---|
| `test_api` | not-runnable | 0 | 0 | 0 |
| `test_compare_apps` | pass | 8 | 0 | 0 |
| `test_compare_emulated` | pass | 7 | 0 | 0 |
| `test_compare_interface` | pass | 17 | 0 | 0 |
| `test_compare_mbb` | pass | 9 | 0 | 0 |
| `test_compare_quic` | pass | 3 | 0 | 2 |
| `test_compare_quic_glue` | pass | 3 | 0 | 0 |
| `test_compare_ro` | pass | 13 | 0 | 0 |
| `test_compare_semantics` | pass | 13 | 0 | 0 |
| `test_compare_tcplite` | pass | 13 | 0 | 0 |
| `test_compare_validation` | pass | 7 | 0 | 0 |
| `test_config` | not-runnable | 0 | 0 | 0 |
| `test_exp_campaign` | pass | 25 | 0 | 0 |
| `test_exp_conditions` | pass | 20 | 0 | 0 |
| `test_exp_export` | pass | 22 | 0 | 0 |
| `test_exp_predictions` | pass | 7 | 0 | 0 |
| `test_exp_stats` | pass | 20 | 0 | 0 |
| `test_kernel_wire_formats` | pass | 5 | 0 | 1 |
| `test_live_kernel_lab` | pass | 12 | 0 | 0 |
| `test_mip_agents` | pass | 24 | 0 | 0 |
| `test_mip_dataplane` | pass | 11 | 0 | 0 |
| `test_mip_demo` | pass | 5 | 0 | 0 |
| `test_mip_ipip` | pass | 8 | 0 | 0 |
| `test_mip_live` | pass | 1 | 0 | 1 |
| `test_mip_messages` | pass | 7 | 0 | 0 |
| `test_mip_sim` | pass | 17 | 0 | 0 |
| `test_mip_udp` | pass | 10 | 0 | 0 |
| `test_ml_evaluate` | pass | 10 | 0 | 0 |
| `test_ml_features` | pass | 10 | 0 | 0 |
| `test_ml_labels` | pass | 9 | 0 | 0 |
| `test_ml_pipeline` | pass | 27 | 0 | 0 |
| `test_ml_splits` | pass | 7 | 0 | 0 |
| `test_netem_config` | pass | 5 | 0 | 0 |
| `test_netem_integration` | pass | 7 | 0 | 7 |
| `test_netem_manager` | pass | 12 | 0 | 1 |
| `test_netem_measure_cli` | pass | 9 | 0 | 0 |
| `test_netem_plan` | pass | 7 | 0 | 0 |
| `test_netem_safety` | pass | 4 | 0 | 0 |
| `test_project_structure` | pass | 3 | 0 | 0 |
| `test_scientific_stack` | pass | 1 | 0 | 0 |
| `test_traffic_loopback` | pass | 7 | 0 | 0 |
| `test_traffic_metrics` | pass | 23 | 0 | 0 |
| `test_traffic_scenario` | pass | 9 | 0 | 0 |
| `test_traffic_termination` | pass | 7 | 0 | 0 |
| `test_traffic_wire_schema` | pass | 6 | 0 | 0 |

Why tests were skipped (all environmental):

| Modules | Reason |
|---|---|
| `test_compare_quic` | aioquic not installed (pip install aioquic) |
| `test_kernel_wire_formats` | ipip link creation result code 95 (0 means this kernel CAN do tunnels) |
| `test_mip_live` | set NETSIM_RUN_PRIVILEGED=1 to run live Mobile IP tests |
| `test_netem_integration` | set NETSIM_RUN_PRIVILEGED=1 to run live network tests |
| `test_netem_manager` | sudo not installed |
<!-- END:tests -->

Not counted above because they cannot run here: `test_api`, `test_config` (need fastapi, pydantic, httpx, pytest), the frontend test (`npm install`),
live suites under `NETSIM_RUN_PRIVILEGED=1`, and the real-aioquic tests (the 2 skips in `test_compare_quic`).

## 6. Measurement provenance
| Value | Origin |
|---|---|
| Every number in `examples/comparison`, `examples/mobileip`, `docs/MOBILITY_COMPARISON.md` section 4 | **Simulated** (virtual time; no real delay/queue/kernel stack). Each result file has a `provenance` block |
| Real-kernel UDP failover (docs/MOBILITY_COMPARISON.md section 7) | **Measured** on a real kernel: 0/600 lost, max gap 5.9–7.0 ms, median RTT 0.1 ms (3 runs; no netem, no mobility protocol) |
| TCP loopback runs | **Measured**, real kernel TCP, no path delay (`timing_comparable: false`) |
| Tunnel overhead (emulator) | **Counted** from real encapsulated bytes |
| Tunnel overhead (live demo) | **Analytic** (20 B), flagged in `provenance.analytic_values` |
| Registration delay / handoff latency | **Simulated** (path delays from the YAML + 1 ms modelled HA processing) |

## 7. Comparability audit (traffic workload and metrics)
Checked by `test_compare_semantics` (WorkloadParity): all four emulated approaches send identical probes (same count, 200-byte size, identical send
schedule at 37 pps), see identical movement times and a 20 ms detection delay, compute RTT from the same echo definition, and deliver the same bytes.
Remaining, documented differences: reliable (TCP/QUIC) vs unreliable (UDP) carriers (loss is not comparable, use the service gap); recovery points are
protocol-defined (latency is not strictly comparable, service gap is); TCP/QUIC use the direct path while basic Mobile IP's forward path goes via the Home Agent; the
comparison topology gives the correspondent direct links to the access networks.

## 8. Defects found by this review (all fixed, each with a regression test)
1. **MBB stale timer**: moving back to a link before an earlier "release old link" timer fired took the new link down (381/550 probes lost).
2. **Stage 4 `handoff_end`** recorded ~1 ms after the move instead of at registration completion.
3. **TcpLite** allowed sending from an address the node no longer owned.
4. **Interface accepted nonsense as `ok`**: `duration_s=0`, negative rate, unknown interfaces, unordered moves, and payloads that exceed the MTU once tunnelled (100 % loss, status `ok`). Now `invalid` / `unsupported` with reasons.
5. **QUIC adapter timer re-armed forever** at the same virtual instant (hang); simulator now raises on an event storm.
6. **Documentation error**: the live testbed does *not* loop stale packets when only the mobile node's link end goes down (real kernel: ICMP host unreachable after ~3 s); it loops only if both ends are down. Both behaviours are now modelled (`link_semantics`).
7. **(Stage 7) TcpLite retransmitted only the head segment on a timeout** although documented as go-back-N; invisible without random loss, but with 0.5 % loss delivery collapsed (52 % of probes undelivered). Fixed (all outstanding segments are resent); the Stage 5 matrix was rerun before/after: every handoff/loss/RTT metric and raw CSV is byte-identical, only internal TcpLite counters of the `stall-timeout` run changed. Regression coverage: `tests/test_exp_conditions.py::BackwardsCompatibility` and the loss runs in `test_exp_campaign.py`. TcpLite still has no fast retransmit/SACK/congestion control, so it is flagged `model_limit_note` under loss.

## 9. Known issues and limitations (prioritised)
| # | Severity | Issue |
|---|---|---|
| K1 | High | QUIC adapter never run against real aioquic; expect API mismatches |
| K2 | High | Live path unverified: `ip`/`tc` syntax, `setpriv`, daemons in namespaces, **kernel ipip tunnels** (this kernel cannot create them), `tc netem` |
| K3 | High | Stage 1 FastAPI app, settings tests (pytest-style, need fixtures) and the frontend have never been run |
| K4 | Medium | Emulator is a model: no queues, bandwidth, ARP, ICMP, fragmentation; TcpLite lacks congestion control/SACK/fast retransmit |
| K5 | Medium | Live make-before-break uses a single tunnel device (benefit limited to link overlap) |
| K6 | Medium | Default emulator link semantics (`both-ends`) differ from the live testbed (`kernel-veth`); delivery metrics are identical, drop accounting differs |
| K7 | Medium | No live TCP/QUIC adapters; loopback environment supports only TCP; no TCP run across real routed namespaces |
| K8 | Low | Mobile IP is educational, not RFC 5944 compliant; the route-optimisation model is not Mobile IPv6 (both documented, with security limits) |
| K9 | Low | No lock file (only `requirements-verified.txt` listing what the tests ran against); kernel-lab tests need Python ≥ 3.12 (`os.unshare`/`os.setns`) |
| K10 | Low | Minor duplication between `mobility_sim.py` and `compare/probe_files.py` recorders |
| K11 | Medium | Stage 7 results are simulator-generated: fixed link delays, independent per-packet loss, no jitter/queues; one topology, traffic load and handoff pattern; one-factor-at-a-time sweeps (no interactions) |
| K12 | Medium | Stage 7: several metrics are identical in every replicate (the emulator is deterministic there); they are reported as deterministic differences, not as statistics. QUIC was not measured (aioquic missing); the 20-replicate standard campaign is defined (`config/experiments/stage7_standard.yaml`) but was not run |
| K13 | Low | The Stage 6 model, applied to Stage 7 conditions, warns inconsistently (see `docs/EXPERIMENTS.md` §10); it was never validated on real data |

## 10. Dependencies
Runtime: `requirements.txt` (fastapi, uvicorn, pydantic, pydantic-settings, numpy, pandas, scikit-learn, matplotlib, PyYAML). Dev: `requirements-dev.txt`
(+ pytest, httpx, ruff). Optional: `requirements-quic.txt` (aioquic). `requirements-verified.txt` = versions the suite actually ran against
(numpy, pandas, scikit-learn, matplotlib, PyYAML, cryptography only). System: iproute2 (+`iproute-tc` on Fedora), iputils-ping, util-linux (`setpriv`, `nsenter`),
`sudo`; kernel modules `veth`, `ipip`, `sch_netem` for the live paths (Fedora may need `kernel-modules-extra`).

## 11. Linux validation commands (run these on a real Linux host)
```bash
make setup && source .venv/bin/activate                 # venv + requirements-dev.txt
pytest                                                   # Stage 1 API/settings tests + everything pytest-collectable
python scripts/run_all_tests.py                          # regenerates docs/TEST_REPORT.md
sudo modprobe ipip sch_netem veth                        # Fedora: sudo dnf install kernel-modules-extra iproute-tc
scripts/check_env.sh                                     # tools + capabilities
scripts/topology.sh create && scripts/topology.sh ping mn 10.10.1.2 -c 20   # expect RTT ~ 40 ms (5+10+5 ms each way, tc netem)
sudo -v && NETSIM_RUN_PRIVILEGED=1 python -m unittest discover -s backend/tests -t backend -p "test_netem_integration.py"
unshare -Urn python3 -m unittest tests.test_live_kernel_lab tests.test_kernel_wire_formats   # from backend/; kernel lab without root
PYTHONPATH=backend python -m app.mobileip demo --execute # live Mobile IP demo (needs ipip)
sudo -v && NETSIM_RUN_PRIVILEGED=1 python -m unittest tests.test_mip_live
pip install -r requirements-quic.txt && python -m unittest tests.test_compare_quic -v   # from backend/
cd frontend && npm install && npm test
```

## 12. Repository state
Git history is not included in the ZIP. Latest commits: see `git log` in the original project (the commit the tests ran on is stated at the top of this file).
