# NEXT_STEPS — exact starting point for Stage 6

**Gate 0 (blocking): Stage 6 has not been started and its specification has not been given.** The only thing known is that a
dashboard is intended and was explicitly postponed. Wait for the user's approval and Stage 6 brief before writing any Stage 6 code.

## A. First 15 minutes in a new chat
1. Unzip, `cd netsim-lab`, read `PROJECT_STATUS.md` (honest state) and `docs/MOBILITY_COMPARISON.md` (what each approach is and is not).
2. Re-establish the baseline on the new machine: `python scripts/run_all_tests.py` — compare with `docs/TEST_REPORT.md` (281 passed / 0 failed / 12 skipped / 2 not runnable in the original sandbox). Anything that was skipped there for environmental reasons may run, and fail, here: that is expected and valuable.
3. Ground rules inherited from the project: never mark untested functionality complete; label simulated/analytic values (`provenance`); no undocumented mocks (the only test double is `backend/tests/stubs/aioquic`, labelled); every bug fix gets a regression test; unsupported scenarios are reported with a reason, never skipped.

## B. Fixes / validation required before Stage 6 (priority order)
| Pri | Task | How to know it is done |
|---|---|---|
| P0 | Install the dev dependencies and run the Stage 1 tests that never ran: `make setup && pytest` | `test_api`, `test_config` pass under pytest; fix the FastAPI app if not |
| P0 | Run the real-library QUIC tests: `pip install -r requirements-quic.txt && cd backend && python -m unittest tests.test_compare_quic -v` and `python -m app.compare run --approach quic-migration` | the 2 skipped tests run and pass; QUIC row in `python -m app.compare matrix` has real numbers. **Expect to adjust `app/compare/quic_sim.py`** (never executed against aioquic). Also check whether a path-validated signal is observable and use it as the recovery point if so |
| P1 | Verify the live Stage 2 lab on a Linux host with iproute2 + sudo: `sudo modprobe veth sch_netem; scripts/check_env.sh; scripts/topology.sh create; scripts/topology.sh ping mn 10.10.1.2 -c 20; scripts/topology.sh destroy` and `NETSIM_RUN_PRIVILEGED=1 python -m unittest tests.test_netem_integration` | RTT ≈ 40 ms (config delays), injected delay/loss measurable, destroy leaves decoy namespaces alone |
| P1 | Verify the live Mobile IP demo with real kernel tunnels: `sudo modprobe ipip`, `PYTHONPATH=backend python -m app.mobileip demo --execute`, `NETSIM_RUN_PRIVILEGED=1 python -m unittest tests.test_mip_live` | tunnels created/re-pointed/removed, probes reach the home address, binding timeline `[A, B, A, None]`, no `tnl-*` left behind. Check `ip tunnel` syntax, the `setpriv` launch and the handshake latency |
| P1 | Run the kernel-lab tests on the target machine (Python ≥ 3.12): `cd backend && unshare -Urn python3 -m unittest tests.test_live_kernel_lab tests.test_kernel_wire_formats` | all pass; if the host kernel supports ipip, `test_this_kernel_cannot_run_ipip_tunnels` reports it (informational skip) |
| P2 | Frontend scaffold: `cd frontend && npm install && npm test` | the one test passes |
| P2 | Re-run the comparison with real `tc netem` delays in the live lab for TCP (and QUIC) to check the emulator's qualitative ordering; add live TCP/QUIC adapters (they only need the probe apps launched in the namespaces) | live numbers recorded with `provenance.data_origin = real-kernel-namespaces` and compared against the emulated ones, differences documented |
| P3 | Decide whether the default emulator link semantics should become `kernel-veth` (what the live demo does); live MBB with two tunnel devices | documented decision; tests updated |
| P3 | Optional cleanup: unify the duplicated recorder code (`mobility_sim.py` vs `compare/probe_files.py`); produce a real lock file (`pip freeze`) | tests still pass |

## C. Where Stage 6 (e.g. the dashboard) would attach — suggestions only, pending the real brief
* **Data contract** (stable, versioned): per run `comparison_metrics.json` (`schema_version`, `provenance`, `handoffs[]`, `handoff_summary`, `loss`, `continuity`, `mobility_overhead`), `experiment.json` (status/reason/spec), raw CSVs described in `schemas/`, and the matrix `comparison.json` (`status` ∈ ok/unsupported/unavailable/invalid/failed). A dashboard should be read-only over these files and must display `provenance.data_origin` and every non-ok status with its reason.
* **Entry points that already exist**: `python -m app.compare list|run|matrix`, `run_experiment()`, `app.compare.matrix.run_matrix()`, `GET /api/health` in `backend/app/main.py` (FastAPI skeleton, config via `NETSIM_*` env vars), `frontend/` (Vite + React scaffold, proxies `/api` to 127.0.0.1:8000).
* **Natural first steps once approved**: (1) add read-only API routes that list runs and serve `comparison_metrics.json` / `comparison.json` from `data/results` (validate paths against the id regex in `app/traffic/schema.py`); (2) a runs/matrix page that shows provenance badges and the unsupported-scenario table; (3) per-run handoff timeline from `approach_events.csv` + `events.csv`; (4) only then triggering experiments from the UI (emulated environment only; live runs need sudo and must stay out of the web process — the project rule is that no network-facing process runs privileged).
* Keep the safety model: the API process must never run `ip`/`tc`; privileged work stays behind `app/netem/runner.py` and its allow-list.

## D. Repository conventions
* Tests are `unittest` (pytest also works for the class-based ones); naming `tests/test_<area>_<topic>.py`; helpers `netem_fakes.py`, `traffic_helpers.py`, `mip_helpers.py`, `kernel_lab.py`; stubs in `tests/stubs/`.
* Generated files: `docs/TEST_REPORT.md`, `docs/test_results.json` (via `scripts/run_all_tests.py`), `examples/*` (via `scripts/generate_*examples.py` and `python -m app.compare matrix --out-dir examples/comparison`). Regenerate them after behaviour changes instead of editing by hand.
* Runtime outputs go to `data/` (git-ignored except `.keep_dir`).
