# STAGE 8 MANIFEST — netsim-lab-stage-8.zip (backend API)

Built on the uploaded `netsim-lab.zip` (Stage 7). File-by-file list with sizes and SHA-256: `STAGE8_MANIFEST.json` (it does not list itself).
The SHA-256 of the archive as a whole is given in the hand-off message, not inside the archive.

## 1. What this archive contains
| Path | Content |
|---|---|
| `backend/app/services/` | **new** framework-independent business logic: settings, validation, catalog, SQLite store, job manager, worker, process launcher, results, exports, model registry, `ExperimentService` |
| `backend/app/api/` | HTTP layer: `schemas.py`, `errors.py`, `deps.py`, `routes/{catalog,experiments}.py` (+ Stage 1 `health.py`); `backend/app/main.py` now a factory with a lifespan handler and POST/DELETE CORS |
| `backend/tests/` | Stage 1-7 tests + **new** `test_api_{validation,store,jobs,results,http}.py`, `api_helpers.py`; `test_project_structure.py` updated (see §5) |
| `config/api/scenarios.yaml` | the 7 network scenarios offered by `GET /api/scenarios` |
| `docs/API.md` | endpoints, request/response schemas, status codes, lifecycle rules, examples, safety summary; `docs/TEST_REPORT.md`, `docs/test_results.json` regenerated |
| `examples/api/stage8/` | real results of 4 API-driven experiments (request, metrics, handoff table, predictions summary, session transcript, history CSV); reproducible byte-for-byte (`scripts/generate_api_examples.py --check` → "reproduced byte-for-byte") |
| `examples/` (rest) | Stage 5-7 shipped examples **as far as they existed** (see §5, upload note) |
| `scripts/` | + `export_openapi.py`, `api_smoke.py`, `generate_api_examples.py`, `build_stage8_archive.py` |
| `schemas/api/README.md` | explains why `openapi.json` is generated, not shipped |
| `.env.example`, `Makefile`, `requirements*.txt`, `README.md`, `PROJECT_STATUS.md`, `NEXT_STEPS.md`, `docs/ARCHITECTURE.md` | updated; dependency manifests unchanged (Stage 8 adds no dependency) |
| Excluded | `.git`, virtualenvs, `node_modules`, `__pycache__`/`*.pyc`, caches, `data/` runtime output (API database, job directories), `.env`, temp files |

## 2. Validation status (actual results, this build)
Command: `PYTHONPATH=backend python3 scripts/run_all_tests.py` (unittest, every module in isolation) — 2026-10-29 21:01 UTC, Python 3.12.3, Linux, uid 0.

| | Result |
|---|---|
| **Whole suite** | 546 tests in 50 modules: **521 passed, 0 failed, 25 skipped, 2 modules not runnable** |
| Stage 8 service layer | `test_api_validation` 22 ✔, `test_api_store` 11 ✔, `test_api_jobs` 31 ✔ (real worker processes, real cancellation, crash/timeout/restart recovery), `test_api_results` 18 ✔ — **82 passed, 0 skipped** |
| Stage 8 HTTP layer | `test_api_http` 13 tests **all skipped**: FastAPI/httpx are not installed here and there is no network. **The FastAPI routes, exception handlers, lifespan, CORS and auth dependency have never been executed**; they were only compiled (`py_compile`) |
| Not runnable (pre-existing) | `test_api`, `test_config` (FastAPI/pydantic/pytest missing) |
| Skipped (pre-existing, environmental) | 12: aioquic missing, kernel without ipip, no sudo/iproute2 (see `docs/TEST_REPORT.md`) |
| Changed regression baseline | Stage 1-7: 438 passed → still 438 passed; 12 skipped unchanged; no previously passing test fails |
| Example reproduction | `python scripts/generate_api_examples.py --check` → reproduced byte-for-byte |
| Archive | built, opened, file list and SHA-256 of every member compared to `STAGE8_MANIFEST.json`, and the quick API tests re-run **from the extracted copy** (result in the hand-off message) |

## 3. Startup commands
```bash
./scripts/setup.sh                 # or: make setup   (venv + dependencies; normal user, never root)
make api-test                      # Stage 8 tests; the HTTP tests need fastapi + httpx (installed by setup)  <- run this first
make run-api                       # uvicorn on 127.0.0.1:8000, docs at /docs, API under /api
make api-openapi                   # write schemas/api/openapi.json from the code
make api-smoke                     # no FastAPI needed: one real experiment through the worker process
PYTHONPATH=backend python scripts/generate_api_examples.py --check
python scripts/run_all_tests.py    # full regression report
```
Settings: `NETSIM_API_*` in `.env.example` (data directory, concurrency, queue, timeouts, retention, limits, model dirs, optional bearer token).

## 4. Known limitations (details: PROJECT_STATUS.md §9, docs/API.md §7)
1. FastAPI layer unexecuted (above) — the highest-risk item; run `make api-test` first and fix what it reports.
2. `schemas/api/openapi.json` is not shipped (cannot be generated without FastAPI); `make api-openapi` creates it.
3. One API process per data directory (file lock); single bearer token, no TLS, no rate limiting; only single experiments are exposed (no campaigns); predictions only for experiments created with `predict: true`.
4. Environments: `emulated` and `loopback` only; `live` (privileged namespaces) is deliberately unreachable. All results are simulator-generated (or real loopback TCP), never live; there is no metric stream, hence **polling, no WebSocket**.
5. Progress is phase-level; Linux-only process-group/`/proc` handling; no memory limit for workers.
6. The shipped Stage 6 model shows no predictive skill beyond the always-alarm reference on simulated data; `GET /api/models/{id}/evaluation` says so.
7. QUIC cannot run here (aioquic missing): the API reports `approach_unavailable` instead of running it.

## 5. Deviations and findings to know about
* **Upload note:** the received zip had no `examples/` directory. 192 files tracked in its own git history were restored with `git checkout -- examples`. The Stage 7 example campaign (raw data of 372 runs, analysis tables, charts, reproduction check) was never tracked (`.gitignore` ignores `*.csv`, `*.json`, `*.png`) and is **absent**: only `examples/experiments/stage7/ledger.jsonl` and `analysis/REPORT.md` exist. It was not regenerated (`make exp-example`). `.gitignore` now re-includes `examples/` and `schemas/`.
* `backend/tests/test_project_structure.py`: the old check ("only a file named `runner.py` may spawn processes") was replaced by an explicit path allow-list (`netem/runner.py`, `services/process_launcher.py`) plus a new test that the API launcher uses a fixed argv, `shell=False`, no sudo, and that nothing else in `api/`/`services/` spawns or forks.
* A race was found by the new tests and fixed: a cancelled experiment's terminal status became visible before its leftovers were cleaned (PROJECT_STATUS defect 8).
* The git history (`.git`) is not included, as in earlier stages.
