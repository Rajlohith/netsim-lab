# NetSim Lab — Stages 1–8: Foundation, Network Emulation, Traffic & Metrics, Basic Mobile IP, Mobility Comparison, Handoff Prediction, Experiments & Analysis, Backend API

Scaffold for a network-emulation + ML project targeting **Zorin OS** and **Fedora Linux**.
Stack: Python, FastAPI, React/JavaScript, Linux network namespaces + iproute2 + `tc netem`,
scikit-learn, Pandas, NumPy, Matplotlib. Stage 2 adds the namespace-based mobility topology; Stage 6 adds handoff-degradation prediction (`python -m app.ml`, simulated data only; see `docs/HANDOFF_PREDICTION.md`); `analysis/` is still a placeholder.

## Prerequisites
| | Zorin OS (Ubuntu-based) | Fedora |
|---|---|---|
| Base | `sudo apt install git python3 python3-venv python3-pip iproute2 iputils-ping` | `sudo dnf install git python3 python3-pip iproute iproute-tc iputils` |
| Frontend | `sudo apt install nodejs npm` (or nvm; Node 18+) | `sudo dnf install nodejs npm` |

Python >= 3.10 is required.

## Setup (as a normal user, never root)
```bash
git clone <repo> netsim-lab && cd netsim-lab   # or use this directory
make setup                 # creates .venv, installs requirements-dev.txt, copies .env
source .venv/bin/activate
make check                 # read-only diagnostics
make test                  # backend tests
make run-api               # http://127.0.0.1:8000/api/health
make frontend-install && make frontend-dev   # http://localhost:5173
```
Frontend tests: `cd frontend && npm test`.

## Stage 2 — network emulation
Topology (defined in `config/topology.yaml`, five namespaces connected by veth pairs):
```
cn --- ha --+-- ana --- mn        cn=Correspondent Node  ha=Home Agent  mn=Mobile Node
            \-- anb ---/          ana/anb=Access Network A/B  (namespaces nsl-cn, nsl-ha, ...)
```
```bash
scripts/topology.sh validate                 # check the YAML, no privileges
scripts/topology.sh plan                     # show every command, no privileges
scripts/topology.sh create                   # asks for sudo once (sudo -v); elevates ip/tc per command
scripts/topology.sh status                   # addresses, routes, ip_forward, qdiscs per node
scripts/topology.sh ping mn 10.10.1.2 -c 20  # RTT + loss from the mobile node to the correspondent node
scripts/topology.sh shape ana-mn --delay-ms 80 --loss 2 --rate-mbit 5   # change netem live
scripts/topology.sh shape ana-mn --clear
scripts/topology.sh destroy                  # removes ONLY nsl-* namespaces from the config/state file
# manual commands inside a node:  sudo ip netns exec nsl-mn ping 10.10.1.2
```
Edit `config/topology.yaml` (addresses, routes, per-link/per-end `netem`: `delay_ms`, `jitter_ms`,
`loss_percent`, `rate_mbit`, `limit_packets`) and re-run destroy/create for a repeatable lab; use a
different `NETSIM_NETNS_PREFIX` or `TOPOLOGY_CONFIG=...` for variants.

Tests: `make test` runs the unit tests (fake host, no root). The live acceptance tests build the real
topology and measure it: `sudo -v && make test-live` (needs `ip`, `tc`, `ping`; uses prefix `nslt-`).
Safety model and the list of privileged commands: [docs/PRIVILEGED_OPERATIONS.md](docs/PRIVILEGED_OPERATIONS.md).

## Stage 3 — traffic and metrics
Sequence-numbered UDP probes (sender + echoing receiver), raw CSV/JSON per run, derived metrics (loss from sequence numbers,
inter-arrival gaps, RTT, jitter, throughput, handoff interruption). Details, definitions and clock limitations:
[docs/TRAFFIC_MEASUREMENT.md](docs/TRAFFIC_MEASUREMENT.md); schemas in `schemas/`; real example outputs in `examples/results/`.
```bash
make test                                    # includes loopback scenarios + SIGTERM/SIGKILL tests (no root)
PYTHONPATH=backend python -m app.traffic analyze examples/results/example-handoff-blackout/example-1
python scripts/generate_example_results.py   # regenerate the examples
# in the namespace lab (sudo): continuous probes MN -> CN while eth-a goes down/up
scripts/topology.sh create
PYTHONPATH=backend python -m app.traffic scenario config/scenarios/handoff_a_to_b.yaml --execute
```

## Stage 4 — basic Mobile IP (educational, **not** RFC 5944 compliant)
Home address + care-of address, signed registration requests/replies with lifetimes, Home Agent bindings, IP-in-IP tunnelling and a
handoff between two foreign networks, with registration delay, tunnel overhead and connectivity interruption recorded.
Implemented vs simplified vs absent, security limitations and demo steps: [docs/MOBILE_IP.md](docs/MOBILE_IP.md).
```bash
make test                                                     # protocol, emulator and real-UDP integration tests (no root)
PYTHONPATH=backend python -m app.mobileip sim --out-dir data/results          # emulated handoff + metrics
PYTHONPATH=backend python -m app.mobileip analyze examples/mobileip/mip-handoff/example-1
PYTHONPATH=backend python -m app.mobileip demo                # live namespace demo: dry-run (add --execute with sudo)
```

## Stage 5 — mobility comparison
TCP reconnection baseline, basic Mobile IP, Mobile IP with make-before-break, a **simplified** route-optimisation model (a Mobile IPv6
feature, not Mobile IPv4) and QUIC connection migration (aioquic, applies only to QUIC applications) behind one experiment interface,
all producing the same raw files and core metrics. Unsupported scenarios are reported with reasons. See
[docs/MOBILITY_COMPARISON.md](docs/MOBILITY_COMPARISON.md); generated matrix in `examples/comparison/comparison.md`.
```bash
PYTHONPATH=backend python -m app.compare list                  # what can run where (and whether aioquic is installed)
PYTHONPATH=backend python -m app.compare matrix --out-dir data/results/comparison
PYTHONPATH=backend python -m app.compare run --approach mip-mbb --hold-ms 100
pip install aioquic                                            # enables the QUIC adapter (untested in the dev sandbox)
```

## Stage 7 — experiments and statistical analysis
Replicated, reproducible comparison campaigns over every registered approach with configurable latency, packet loss, Home Agent distance,
handoff timing and registration conditions; run table with seeds/versions/conditions, failed/incomplete/interrupted runs kept and reported,
medians with exact confidence intervals, matched-pair tests with Holm correction, CSV/JSON/Markdown exports, charts and Stage 6 predictions
recorded next to the measurements. All values are simulator-generated. Full guide: [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md); shipped
results: `examples/experiments/stage7/analysis/REPORT.md`.
```bash
PYTHONPATH=backend python -m app.compare plan --config config/experiments/stage7_example.yaml        # design, no run
PYTHONPATH=backend python -m app.compare campaign --config config/experiments/stage7_example.yaml \
        --out-dir data/experiments/stage7-example --predict-model examples/ml/stage6/model             # ~15-35 min on one core; resumable
PYTHONPATH=backend python -m app.compare analyze examples/experiments/stage7                           # statistics from saved results
PYTHONPATH=backend python -m app.compare plots examples/experiments/stage7                             # charts
PYTHONPATH=backend python -m app.compare reproduce examples/experiments/stage7 --sample 10             # byte-for-byte re-run check
```

## Stage 8 — backend API
FastAPI backend that starts experiments as **managed background jobs** (separate worker processes, SQLite metadata, queue, concurrency limit,
duplicate-run prevention, safe cancellation, restart recovery), and serves status, metrics, handoff events, Stage 6 predictions, history and
JSON/CSV/ZIP exports of the real results. Polling, not WebSocket (runs produce metrics only when they finish). The privileged `live`
environment is never reachable through the API. Full reference: [docs/API.md](docs/API.md).
```bash
make run-api                                  # http://127.0.0.1:8000/docs  (unprivileged)
curl -s -X POST localhost:8000/api/experiments -H 'content-type: application/json' -d '{"approach":"mip-basic","scenario":"lossy-access","seed":1}'
make api-smoke                                # same service layer without HTTP (no FastAPI needed): one real run through a worker process
make api-test                                 # Stage 8 tests (the HTTP tests need fastapi + httpx; they are skipped otherwise)
```
Settings: `NETSIM_API_*` variables in `.env.example`. Existing CLI workflows (`python -m app.compare ...`) are unchanged.

## Project status and tests
Read **[PROJECT_STATUS.md](PROJECT_STATUS.md)** (what is implemented, simulated or untested, test results, known issues) and
**[NEXT_STEPS.md](NEXT_STEPS.md)**. Reproduce the test report with `python scripts/run_all_tests.py` (unittest only, no pytest needed);
it writes `docs/TEST_REPORT.md` and `docs/test_results.json`. Live kernel tests run as root or under `unshare -Urn`.

## Layout
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Safety / privileges
- The app never runs as root; Stage 2 changes only network namespaces it creates (never host firewall rules, routes or interfaces).
- Privileged operations (namespaces, veth, `tc netem`) are allow-listed and documented in
  [docs/PRIVILEGED_OPERATIONS.md](docs/PRIVILEGED_OPERATIONS.md): prefixed objects only,
  dry-run unless `--execute`, no changes to the host's default namespace.

## Configuration
Environment variables prefixed `NETSIM_` (see `.env.example`).
