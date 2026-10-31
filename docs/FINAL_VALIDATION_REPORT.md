# Final validation report — Stage 10

Written from commands actually executed on **2026-10-31** in the build sandbox: Linux 6.18 (Firecracker VM), Python 3.12.3, Node 22.22, uid 0, **no network**, 1 CPU.
Missing there: fastapi, uvicorn, pydantic, pydantic-settings, httpx, pytest, aioquic, ruff, vite, vitest, jsdom, @testing-library, `ip`, `tc`, `ping`, `sudo`.
Present: numpy 2.4.4, pandas 3.0.2, scikit-learn 1.8.0, matplotlib 3.10.8, PyYAML, cryptography, esbuild 0.27.7, React 19.2.5 (project declares ^18.3.1), Playwright + Chrome 131, `unshare`.
Nothing is reported as passed that did not run. "Not run" always means "environment blocker", never "assumed OK".

## 1. Acceptance checklist
Status words: **verified** (executed, evidence below), **partially verified**, **blocked** (environment), **not implemented**.

| # | Item | Status | Evidence / what is missing |
|---|---|---|---|
| 1 | Reproducible environment setup | **partially verified** | `requirements*.txt`, `make setup`, `scripts/check_env.sh`, `.env.example` exist. `make setup` / `npm install` **blocked** (no network), so a clean-machine install was never executed; `package-lock.json` still absent. Python scientific stack verified at the versions in `requirements-verified.txt`. |
| 2 | Supported simulation execution | **verified** | CLI run (`app.compare run`), 372-run campaign, API worker runs (smoke + browser). QUIC adapter **blocked** (no aioquic). Live namespace/`tc`/IP-in-IP runs **blocked** (see §5). |
| 3 | Experiment configuration + raw-result retention | **verified** | `plan.json`, `campaign.json`, `ledger.jsonl`, 372 raw run dirs (sent/echoes/received/events CSV + `comparison_metrics.json`) shipped in `examples/experiments/stage7/`. Stage 9's upload lacked the raw runs; regenerated here (§3). |
| 4 | Statistical analysis and export | **verified** | `analyze` from saved data regenerates `REPORT.md` identically; CSV/JSON/Markdown + 24 charts. Statistics code is covered by `test_exp_stats` (checked against known values). |
| 5 | Prediction training + held-out evaluation | **verified (with a negative scientific finding)** | Pipeline re-run from the shipped dataset gives identical `results.json`. Held-out split by scenario family. Skill is generator-regularity only (`docs/HANDOFF_PREDICTION.md` §7). Raw→dataset regeneration not repeated. |
| 6 | API functionality and job-state handling | **partially verified** | Service layer (validation, SQLite state machine, job manager, real worker processes, cancel, timeout, crash, restart recovery, exports): the `test_api_*` service-layer modules, which ran and passed, plus real runs through the stdlib adapter. **FastAPI routes/pydantic/CORS/bearer dependency never executed** (14 `test_api_http` tests skipped; `test_api`, `test_config` not runnable). |
| 7 | Dashboard integration with real backend data | **partially verified** | Real Chrome + real service layer: 31/31 checks (§4). Through the **stdlib adapter, not FastAPI**; bundle by esbuild, not Vite. |
| 8 | Test results and known limitations | **verified** | §2 and §6; `docs/TEST_REPORT.md`, `docs/test_results.json` regenerated in this stage. |
| 9 | Demonstration instructions | **verified** (commands that could run were run) | `docs/DEMO.md`; start/stop via `scripts/stack.sh` tested only with stand-ins. |
| 10 | Cleanup after success / failure / cancel / interrupt | **verified for the API/worker path and the namespace plans; partially for the live lab** | §3. |
| 11 | Privileged operations, validation, concurrency, isolation, secrets audit | **partially verified** | §5 (code reading + tests; no penetration test, HTTP layer not executed). |
| 12 | Cross-view data consistency | **verified for the browser/adapter path** | §4. |

## 2. Exact test commands and results
| Command | Result |
|---|---|
| `python3 scripts/run_all_tests.py` (one unittest process per module, `-W error::ResourceWarning`) | **552 ran, 526 passed, 0 failed, 0 errors, 26 skipped; 51 modules; 2 not runnable** (`test_api`: fastapi missing; `test_config`: pydantic missing) |
| skips (26) | `test_api_http` 14 (fastapi/httpx), `test_netem_integration` 7 (needs `NETSIM_RUN_PRIVILEGED=1`), `test_compare_quic` 2 (aioquic), `test_kernel_wire_formats` 1 (kernel `ipip` refused, errno 95), `test_mip_live` 1 (privileged flag), `test_netem_manager` 1 (no sudo) |
| `cd frontend && npm run verify:bundle` | bundle OK, main.js 276.6 kB, main.css 7.4 kB, 42 modules |
| `cd frontend && npm run verify:sandbox` (esbuild + vitest shim, **not vitest**) | **90 passed, 0 failed, 7 skipped** (integration file, needs live URL); `interaction.test.jsx` **not run** (needs jsdom/RTL) |
| `python3 scripts/e2e_browser.py` | **31 passed, 0 failed** (`docs/e2e_browser_results.json`) |
| `python -m app.compare campaign --config config/experiments/stage7_example.yaml …` | 372 planned, 372 ok |
| `python -m app.compare reproduce <dir> --sample 10` | checked 10, identical 10, mismatches [] |
| `python -m app.compare analyze examples/experiments/stage7` | report identical to previous (timestamps ignored) |
| `python -m app.ml pipeline --dataset-dir examples/ml/stage6/dataset` | `results.json` identical to shipped |
| `scripts/api_smoke.py` (with `NETSIM_API_ALLOW_ROOT=1`) | job completed; without the override it refused to start as root, as designed |
| **Not run (blocked):** `npm test` (vitest), `npm run build` (vite), `make api-test` HTTP tests, `make test-live`, `make test-live-mip`, `ruff`, QUIC, real uvicorn/Vite start via `stack.sh` | blockers listed above |
Count note: Stage 9's documents reported 551 ran / 25 skipped; this run counts 552 / 26 (14 skipped `test_api_http` cases). The one-test difference was not investigated.

## 3. Cleanup verification (tests that ran and passed)
* Success/failed/timeout/crash/kill/cancel/restart of API jobs (`test_api_jobs`): `cancel_queued_is_immediate_and_never_starts`, `cancel_running_kills_the_process_group…`, `sigterm_ignoring_worker_is_killed_after_grace`, `timeout_is_failed_with_code_timeout`, `worker_crash_without_result_is_incomplete_and_flagged`, `worker_killed_from_outside_is_incomplete`, `launch_failure_is_failed`, `cancelling_a_real_long_experiment_stops_the_real_worker`, `restart_marks_running_incomplete_keeps_queued_and_kills_orphan`, `restart_with_pending_cancel_ends_cancelled`, `pid_reuse_is_not_mistaken_for_our_worker`, `retention_sweep…`.
* Browser path (new): a 120 s experiment stopped with the page's *Stop* button ended `cancelled`; no process mentioning its id remained; after the whole run no process referencing the data dir remained.
* Probes: SIGTERM finalises, SIGKILL leaves a detectably incomplete run (`test_traffic_termination`).
* Namespaces: `destroy` removes only project namespaces, is idempotent, refuses tampered state; failed create rolls back, on a fake host **and on the real kernel** (`test_netem_manager`, `test_live_kernel_lab`).
* Not verified: cleanup of live `tc`/tunnel state (no `tc`, no `ipip`), cleanup after Ctrl-C of a *foreground* live demo.

## 4. Consistency of dashboard, API, exports and saved analysis
Checked in the browser run, same experiment id: lost/sent probe counts on screen = API `metrics.loss`; handoff rows = API handoffs (ids and interruption values); JSON export downloaded through the UI is **byte-identical** to `GET …/export?format=json`; handoffs CSV row count = API handoffs; model page shows the model id returned by `/api/models`; topology page lists every node from `/api/topology`; the approach selector equals `/api/approaches`; the history export contains the experiment. Server and campaign analysis read the same raw run files (`comparison_metrics.json` schema shared). Not checked: that the numbers in a *campaign* run equal those of an API run of identical parameters (both use the same runner and seed rules, but no test compares them directly).
Defects found by this check and fixed: event-timeline caption units (`0.000 s – 0.000 s`), top bar stretching on short pages. One 404 seen by the browser is the documented `predictions_not_available` answer for a run started without predictions.

## 5. Audit (read-through plus existing tests)
| Area | Finding |
|---|---|
| Privileged operations | Only the `netem` runner spawns privileged processes (`sudo -n` per command, allow-list in `netem/safety.py`). The API's only spawn is `services/process_launcher.py` (fixed argv, `shell=False`, own process group, filtered env). Requests for the `live` environment are rejected with `environment_not_exposed` (message: privileged operations are never exposed through the API); the worker gets a minimal environment (PATH, PYTHONPATH, locale, HOME, thread limits), so the API token is not inherited. No `shell=True`/`os.system`/`eval` anywhere in `backend/app`. |
| Request validation | Whitelisted approaches/scenarios/environments, numeric limits (duration, rate, probes), label length, idempotency-key regex, experiment id regex `^exp-[0-9a-f]{16}$` before any path is built; 22 validation tests passed. pydantic request parsing itself **not executed**. |
| Concurrency | max concurrent jobs, bounded queue (429), duplicate-active fingerprint guard (partial UNIQUE index), one manager per data dir, every write inside `BEGIN IMMEDIATE`, partial UNIQUE indexes on active fingerprints and idempotency keys. Tested with real processes. Refuses to run as root unless overridden. |
| Network isolation | Namespaces/interfaces carry the `nsl-` prefix; destroy touches only state-file entries with that prefix; no host default-route or firewall change in any plan (tests assert it). Live isolation on `tc`/tunnels unverified. |
| Secrets | Bearer token compared with `hmac.compare_digest`; `.env` is git-ignored, `.env.example` holds no secret; grep found no credentials. `mobileip/ro_sim.py` contains the constant `RO_SECRET`, labelled `DO-NOT-USE` (simulation only). The server defaults to 127.0.0.1 with no token, so **anyone on the local machine can use the API**; set `NETSIM_API_TOKEN` otherwise. The dashboard cannot enter a token. |
| Other | SQL is parameterised; sort columns are allow-listed. CORS default allows only `http://localhost:5173`. Dev server proxying is not production hosting. |

## 6. Known limitations
* FastAPI layer never executed in any stage: **first thing to run on a normal machine is `make setup && make api-test && make frontend-install && make frontend-test && make frontend-build`.** Fixes may be needed.
* Vite build, vitest, jsdom interaction tests, `package-lock.json`: not run/created. React 19 was used for tests; the project declares 18.
* No live metrics (results appear after a run ends; polling). No jitter in comparison metrics. No token entry UI.
* All results are simulator-generated; loopback and live are different environments. Zero-width confidence intervals appear where the simulator is deterministic.
* Mobile IP is educational, not RFC 5944; route optimisation is a simplified model of a Mobile IPv6 feature; TcpLite is a model; QUIC never run against real aioquic.
* Prediction: no skill beyond generator regularities; threshold baseline is below always-alarm; few scenario families held out.
* WSL2: the emulated/loopback path should work; namespaces need a kernel with `CONFIG_NET_NS`; `tc netem` needs `sch_netem` (often missing in the stock WSL2 kernel); `ipip` tunnels often missing; `sudo -n` must be passwordless for the lab. None of this was tested here.
* Browser coverage: Chrome 131, 1280x900 only.
* The two empty/unused items: `backend/app/analysis/` (empty package) and `STAGE*_MANIFEST` files of earlier stages (kept for history).

## 7. Reproduction recipe
1. Extract the zip, `make setup`, `make frontend-install`.
2. `make stage10-verify` (offline-capable parts) and, with dependencies, `make api-test frontend-test frontend-build`.
3. `make e2e-browser` (needs playwright + Chrome + esbuild + `NODE_PATH` with react/react-dom/esbuild).
4. `make exp-example` to regenerate the campaign (seeds fixed in the config), then `make exp-reproduce DIR=data/experiments/stage7-example`.
