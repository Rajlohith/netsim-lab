# End-to-end workflow and repeatable demonstration (Stage 10)

Every command below was run during Stage 10 unless it is marked **(not run here)**. Outputs quoted are copied from those runs.
All results are **simulator-generated** (deterministic virtual-time emulator) or loopback; none describe a real network.

## 0. Environment
```bash
# Zorin OS / Ubuntu                         # Fedora
sudo apt install git python3 python3-venv   # sudo dnf install git python3 python3-pip
sudo apt install nodejs npm                 # sudo dnf install nodejs npm
make setup            # .venv + requirements-dev.txt + .env   (normal user, needs network)   (not run here: no network)
make frontend-install # npm install                                                           (not run here: no network)
make check            # read-only diagnostics
```
Without root, namespaces/`tc` are not needed for anything in sections 1-4; those use the emulator and the service layer.
The API refuses to run as root (`NETSIM_API_ALLOW_ROOT=1` only in a disposable container; the Stage 10 sandbox was root, so it was set there).

## 1. Start / stop
```bash
make stack-start      # scripts/stack.sh: API on 127.0.0.1:8000 (no --reload) + Vite on 127.0.0.1:5173; logs in .run/logs/
make stack-status     # running/stopped per service + GET /api/health
make stack-stop       # TERM to the process group, KILL after NETSIM_STOP_GRACE_S (default 8 s)
```
`stack.sh` logic (pid-file validation, stop, SIGKILL fallback, refusal messages) was tested with stand-in processes. Starting real uvicorn/Vite
was **not run here** (not installable). Fallback if the script is not suitable: `make run-api` and `make frontend-dev` in two terminals.

## 2. One experiment, end to end
**a. Configure and run (CLI, no server):**
```bash
PYTHONPATH=backend python -m app.compare run --approach mip-basic --seed 1 --duration 12 --out-dir data/demo
# -> run directory: data/demo/mip-basic-emulated/run-1      (raw: sent.csv echoes.csv received.csv events.csv approach_events.csv + comparison_metrics.json)
```
**b. Same through the API/dashboard:** open http://localhost:5173/#/configure, choose *Mobile IP (basic)*, scenario *baseline*, seed 1, *Start experiment*.
The page moves to `#/live/exp-…`, polls the lifecycle (queued → running → completed), then shows the measurements. Equivalent API call:
```bash
curl -s -X POST localhost:8000/api/experiments -H 'content-type: application/json' -d '{"approach":"mip-basic","scenario":"baseline","seed":1}'
```
**c. Raw results are preserved** per experiment in `data/api/exp-<id>/` (raw CSV/JSON + `comparison_metrics.json`, SQLite row in `data/api/*.db`; ZIP export = `GET /api/experiments/{id}/export?format=zip`).

**d. Expected output, mip-basic / baseline / seed 1** (the service-layer smoke run `make api-smoke`, duration 10 s, and the browser run, duration 12 s, both seed 1):
| | smoke (10 s) | browser run (12 s) |
|---|---|---|
| handoffs | 2, all recovered | 2, all recovered |
| packets lost / sent | 16 / 950 (1.684 %) | 16 / 1,150 (1.39 %) |
| RTT median | 33 ms | 33 ms (p95 67 ms) |
| handoff latency mean / max | 71 / 91 ms | 71 / 91 ms |
| service gap max | 110 ms | 110 ms |
| mobility overhead | 20 B/packet | 20 B/packet |
Same seed ⇒ same values (checked: 10/10 sampled runs byte-identical on re-execution, section 3).

## 3. Replicated campaign, statistics, export (shipped, reproducible)
```bash
make exp-plan                                   # design, no run
make exp-example                                # 372 runs, all ok, ~15-20 min on one core here; resumable (writes data/experiments/stage7-example)
make exp-analyze DIR=examples/experiments/stage7   # statistics regenerated from SAVED raw results
make exp-plots   DIR=examples/experiments/stage7
make exp-reproduce DIR=examples/experiments/stage7  # re-run 10 sampled runs, byte comparison
```
Stage 10 results: regenerated 372/372 runs OK; ledger outcomes and prediction summaries identical to the Stage 7 ledger (0 differences);
`REPORT.md` identical to the Stage 7 report apart from timestamps; `reproduce --sample 10` → `checked 10, identical 10, mismatches []`;
`analyze` on the shipped raw data reproduces the same report. Seeds: `base_seed 20261028`, per-run seeds derived in `plan.json`.
Baseline medians (12 replicates per approach, simulator-generated; see `analysis/REPORT.md` for CIs, tests and the caveats):
| approach | app stall median (ms) | probe loss median (%) |
|---|---|---|
| mip-mbb | 20.0 | 0.00 |
| mip-basic | 80.0 | 1.22 |
| tcp-reconnect-notify | 104.75 | 0.00 (reliable transport; loss shows as delay) |
| ro-model | 160.0 | 2.61 |
| tcp-reconnect-stall-timeout | 636.25 | 0.00 |
These describe the emulator under its stated parameters. Several cells have zero-width intervals because the simulator does not vary there; the report says so.
Charts: `examples/experiments/stage7/analysis/plots/` (24). Tables/exports: `summary.csv/json`, `runs.csv/json`, `comparisons.*`, `vs_baseline.*`, `handoffs.csv`.

## 4. Prediction (Stage 6) — training and held-out evaluation
```bash
PYTHONPATH=backend python -m app.ml pipeline --dataset-dir examples/ml/stage6/dataset --out-dir data/ml/rerun
```
Re-running training and evaluation from the shipped dataset gave `results.json` identical to the shipped one (timestamp/path keys ignored).
Raw → dataset regeneration (`make ml-generate`, ~8 min) was **not** repeated in Stage 10. Held-out test (split by scenario family; 120 runs):
| model | F1 | always-alarm reference F1 |
|---|---|---|
| threshold baseline | 0.323 | 0.388 (**below** reference) |
| logistic regression | 0.436 | 0.388 |
| random forest | 0.452 | 0.388 |
Per-dwell-distribution breakdown shows logistic regression falls **below** the reference when dwell times are exponential (0.372 vs 0.410).
The simulator has no gradual pre-handoff degradation, so any skill comes from generator regularities, not from radio-like signals. Do not present it as a handoff predictor for real networks.

## 5. Dashboard walk-through (what the browser check exercised)
`make e2e-browser` builds the bundle, starts the real service layer behind a stdlib HTTP adapter, and drives headless Chrome through: topology → configure
(invalid duration rejected, valid start) → live (results, handoff table, exports) → cancel a 120 s run with *Stop* → predictions, compare, models, history → unknown route.
31/31 checks passed (`docs/e2e_browser_results.json`); screenshots `docs/demo/1-…7-*.png`. Manual demo order for an audience: 1 topology, 2 configure+start, 3 live, 7 history (exports), 5 compare (run two approaches first), 6 models (read the caveats aloud).

## 6. Privileged lab (optional; **not run here**)
`scripts/topology.sh create` / `make test-live` need a normal user with sudo, `ip`, `tc` (`sch_netem`) and `ping`. Namespaces/veth/routing/cleanup were verified
through the project's own command plans on the sandbox's real kernel by netlink (`tests/test_live_kernel_lab.py`); `tc netem` and IP-in-IP were not available.
