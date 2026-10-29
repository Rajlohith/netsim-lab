# STAGE7_MANIFEST

ZIP: `netsim-lab-stage-7.zip` (entire updated project, stages 1-7; top-level folder `netsim-lab/`).

## What Stage 7 added
* `backend/app/compare/{campaign,analysis,stats,plots,reproduce}.py` - replicated, resumable campaigns; statistics; CSV/JSON/Markdown exports; charts; byte-for-byte reproduction check (extends the Stage 5 runner; `cli.py` gained `plan`, `campaign`, `analyze`, `plots`, `reproduce`, `sample-size`, `list --json`, and condition flags on `run`)
* `backend/app/netem/conditions.py` and seeded optional per-link random loss in `backend/app/mobileip/simnet.py` (off by default: Stage 1-6 results unchanged)
* `backend/tests/test_exp_*.py` (94 tests), `config/experiments/*.yaml`, `docs/EXPERIMENTS.md`, Makefile targets `exp-*`
* `examples/experiments/stage7/` - the shipped example campaign: raw data of 372 runs, ledger, plan, analysis tables (CSV+JSON), `analysis/REPORT.md`, 24 charts, `reproduction_check.json`

## Validation performed (actual results)
* **Full suite** `python3 scripts/run_all_tests.py`: 450 ran: 438 passed, 0 failed, 12 skipped; 2 modules not runnable (test_api, test_config: fastapi/pydantic missing, same as Stage 6)
* **New tests**: test_exp_stats=20, test_exp_conditions=20, test_exp_campaign=25, test_exp_export=22, test_exp_predictions=7 (total 94, all passed). Baseline before Stage 7: 356 ran / 344 passed / 12 skipped.
* **Development note**: one full-suite run had 1 failure in a new test (single-replicate cells wrongly labelled "deterministic"); fixed (>= 6 pairs required), regression tests added, suite rerun clean.
* **End-to-end experiment**: `PYTHONPATH=backend python3 -m app.compare campaign --config config/experiments/stage7_example.yaml --out-dir examples/experiments/stage7 --predict-model examples/ml/stage6/model` -> 372/372 runs `ok`; 224 approach comparisons, 156 vs-baseline tests, 31 prediction cells, 24 charts.
* **Reproduction**: `python3 -m app.compare reproduce examples/experiments/stage7 --sample 0` -> 372/372 runs byte-identical.
* **Analysis regeneration from saved results**: tested (everything byte-identical except the timestamp in `analysis_meta.json`; no simulation is run).
* **Backwards compatibility**: Stage 5 example runs reproduced byte-for-byte with default conditions (tested). Stage 5 `matrix` before/after the TcpLite fix: all handoff/loss/RTT metrics and raw CSVs identical; only internal TcpLite counters of `tcp-reconnect/stall-timeout` changed.
* **Defect fixed**: TcpLite retransmitted only the head segment on a timeout (documented go-back-N); invisible without random loss, collapsed delivery with it. Still a simplified model: no fast retransmit/SACK/congestion control.

## Not run / not claimed
* QUIC migration (aioquic not installed): reported as unavailable in every condition
* live / real-namespace environments (no root, iproute2, ipip)
* FastAPI app tests and the frontend (packages missing)
* the 20-replicate standard campaign (config/experiments/stage7_standard.yaml, ~1,680 runs): defined, not executed
* scikit-learn versions other than 1.8.0

Claims not made:
* no result is a measurement of a real network (all simulator-generated)
* no overall ranking or 'superior' approach: findings are per metric and per simulated condition
* the Stage 6 model shows no demonstrated predictive skill on these data
* one-factor-at-a-time design: no interaction effects

## Known limitations
See `docs/EXPERIMENTS.md` section 9 and `PROJECT_STATUS.md` K11-K13. In short: fixed link delays and independent loss only; one topology / traffic load / handoff pattern; one-factor-at-a-time; many metrics are identical across replicates (reported as deterministic differences, not as statistics).

## Files changed relative to Stage 6
Modified (15): `Makefile`, `NEXT_STEPS.md`, `PROJECT_STATUS.md`, `README.md`, `backend/app/compare/approaches.py`, `backend/app/compare/cli.py`, `backend/app/compare/experiment.py`, `backend/app/compare/runner.py`, `backend/app/compare/tcplite.py`, `backend/app/compare/transport_lab.py`, `backend/app/mobileip/mobility_sim.py`, `backend/app/mobileip/simnet.py`, `docs/ARCHITECTURE.md`, `docs/TEST_REPORT.md`, `docs/test_results.json`.
Added: 14 code/test/doc/config files (listed in `STAGE7_MANIFEST.json` -> `changes_vs_stage_6`) plus the example results. Removed: none.

Not included: virtual environments, node_modules, caches, secrets, git history, `data/`.

6761 files are listed with sha256 in `STAGE7_MANIFEST.json` (the two manifest files themselves are not self-hashed). `STAGE6_MANIFEST.*` is kept as history.
