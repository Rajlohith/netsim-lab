# Architecture (through Stage 5)

```
netsim-lab/
├── backend/app/
│   ├── main.py, config.py, api/     FastAPI (unprivileged), /api/health
│   ├── netem/                       Stage 2: network emulation
│   │   ├── topology_config.py       YAML -> validated dataclasses (NetemSpec, Link, Route, ...)
│   │   ├── names.py                 namespace / interface naming rules
│   │   ├── plan.py                  pure command builders (no execution)
│   │   ├── safety.py                allow-list applied to every argv
│   │   ├── runner.py                ONLY place that spawns processes (sudo -n per command)
│   │   ├── topology.py              create / destroy / status / shape / ping orchestration
│   │   ├── measure.py               ping output parser
│   │   └── cli.py, __main__.py      python -m app.netem <command>
│   ├── traffic/                     Stage 3: measurement only (no protocol logic)
│   │   ├── wire.py schema.py        packet format, raw-data schema
│   │   ├── sender.py receiver.py    UDP probe tools (CSV/JSON out, clean shutdown)
│   │   ├── recorder.py shutdown.py  crash-tolerant writers, signal/stop-file handling
│   │   ├── loader.py metrics.py analyze.py   raw files -> metrics.json (no fabrication)
│   │   ├── loopback_proxy.py        userspace impairment proxy (tests/examples, no root)
│   │   └── scenario.py              orchestrates probes + handoff in the netns lab
│   ├── mobileip/                    Stage 4: educational Mobile IPv4-style demo (see docs/MOBILE_IP.md)
│   │   ├── messages.py ipip.py      registration wire format + auth/replay ids; IPv4/UDP/IP-in-IP bytes
│   │   ├── home_agent.py mobile_node.py binding.py   protocol cores (pure logic, injected clock/data plane)
│   │   ├── dataplane.py applier.py  data-plane interface, ip command plans, file handshake, controller
│   │   ├── simnet.py mobility_sim.py  discrete-event emulator + the handoff scenario (deterministic)
│   │   ├── runtime.py demo.py cli.py  real-UDP drivers, live namespace demo, CLI
│   │   └── events.py metrics.py analyze.py   mip_events*.csv -> mip_metrics.json
│   ├── compare/                     Stage 5: common experiment interface (see docs/MOBILITY_COMPARISON.md)
│   │   ├── experiment.py runner.py  ExperimentSpec, run_experiment (ok/unsupported/unavailable/failed), adapters
│   │   ├── approaches.py            registry: what each approach is/is not, support per environment
│   │   ├── apps.py                  shared stream workload + resume protocol (TCP baseline, QUIC)
│   │   ├── tcplite.py transport_lab.py  simplified TCP model + emulated lab for TCP/QUIC
│   │   ├── loopback.py              same app logic over REAL kernel TCP (validation)
│   │   ├── quic_sim.py              aioquic in virtual time (needs aioquic; unverified here)
│   │   ├── events.py normalize.py matrix.py cli.py   common events -> comparison_metrics.json -> matrix
│   ├── mobileip/ro.py ro_sim.py     simplified route-optimisation model (+ MBB in mobility_sim.py / demo.py)
│   ├── ml/  analysis/               [later stages] placeholders
│   └── tests/                       unit tests (fake host), real-kernel lab (kernel_lab.py netlink executor), gated live tests, stubs/
├── config/topology.yaml, scenarios/ lab definition + handoff scenarios (repeatable)
├── schemas/  examples/results/      raw-data schemas, real example runs (loopback)
├── examples/mobileip/               emulated Mobile IP runs (raw + metrics)
├── examples/comparison/             generated comparison matrix + one run directory per scenario
├── config/topology_ro.yaml          Stage 2 lab + direct cn-ana / cn-anb links (comparison topology)
├── scripts/                         setup.sh, check_env.sh, topology.sh, run_all_tests.py (generates docs/TEST_REPORT.md)
├── frontend/                        React + Vite scaffold
└── docs/                            ARCHITECTURE.md, PRIVILEGED_OPERATIONS.md
```

## Topology
```
 cn (correspondent) --- ha (home agent, router) --+-- ana (access net A, router) --- mn (mobile node)
   10.10.1.0/24            10.10.2.0/24 ---------+    10.10.4.0/24
                           10.10.3.0/24 --- anb (access net B, router) --- 10.10.5.0/24 --- mn
```
Namespaces: `nsl-cn nsl-ha nsl-mn nsl-ana nsl-anb`. `mn` has two interfaces (`eth-a`, `eth-b`) and two
default routes (A metric 100 preferred, B metric 200 standby). Handover logic is not part of Stage 2.
