# Baseline and fixes (review pass 1)

Measured in a fresh Linux sandbox (Python 3.12.3, Node 22, running as root, no network namespaces). Re-run on your Fedora machine to confirm.

## Baseline before any change
| Check | Result |
|---|---|
| Backend tests | 11 failed, 14 errors, from three root causes below |
| Frontend tests | 101 passed, 1 failed, 7 skipped (matches your report) |
| Frontend build | passes |
| npm audit | 6 findings (3 moderate, 1 high, 2 critical), all in dev tooling: vite, esbuild, vitest, vite-node, tinypool |

## Root causes found
1. **QUIC missing.** `aioquic` was not declared in `requirements.txt`, so a fresh install never had it. The approach table also marked QUIC `unverified`. Detection (`quic_available()`) is correct: the option is shown but disabled with "not runnable on this server" when aioquic is absent.
2. **scikit-learn drift.** The shipped model bundle was trained with scikit-learn 1.8.0; requirements allowed any version >= 1.4. With 1.9.1 the model registry (correctly) refuses the pickle, which failed every prediction test. Now pinned to `>=1.8,<1.9`.
3. **pydantic error locations.** Newer pydantic appends the union branch to the error location (`duration_s.int`). The API now reports the field name (`duration_s`).

## Changes made
- `requirements.txt`: added `aioquic`, pinned scikit-learn.
- `backend/app/compare/approaches.py`: QUIC emulated status `unverified` -> `supported` (runnable only when aioquic is installed). Basis: a 20 s emulated run completed with aioquic 1.8.0, one connection kept across the handoff, 0 lost of 1950 probes. This is one run, not a full campaign.
- `backend/app/api/errors.py`: strip union-branch tags from error fields.
- `backend/tests/test_exp_campaign.py`: the test assumed aioquic is absent; it now checks both cases.
- Frontend: the header status sits in a labelled group, and the polling test queries it. No status information was removed.

## Results after fixes (sandbox)
- Backend: 2 failures remain. `test_not_running_as_root` fails only because the sandbox runs as root. One live-kernel test failed once in an earlier run and needs namespaces; both need re-checking on your machine.
- Frontend: 102 passed, 7 skipped, 0 failed.

## Review pass 2: redesign
- New design system (tokens, light and dark themes, persisted toggle, pre-paint script), sidebar shell with Workspace / Experiments / Research groups, responsive menu.
- New pages: Overview (real counts, topology, recent runs) and Campaign analysis (describes the CLI workflow; there is no campaign API).
- Configure: advanced settings collapsed, live summary. Results: five headline values and two charts, the rest under Advanced. Compare, topology and history reorganised. See `FEATURE_DECISIONS.md`.
- npm: vite 5 -> 7, vitest 2 -> 3, plugin-react 4 -> 5. Audit 6 -> 3 findings, all dev-only (`@vitest/mocker`, `tinypool`); production dependencies show 0. Vite 7 needs Node >= 20.19. The remaining findings need vitest 4; an install attempt failed with an npm error and was not pursued.
- Verified in headless Chromium against the real API: QUIC selectable and run from the UI, results, compare (condition warning shown), history, deep links, back navigation, dark theme persisted, mobile menu with no horizontal scroll.
- Frontend 102 passed, 7 skipped; production build passes.

## Not verified
- Cancellation, exports and seed reproducibility were covered only by the existing backend tests, not re-checked through the new UI.
- Screenshots were checked for overview, configure and results; compare, history and Research pages were exercised but not inspected visually.
- No Fedora or namespace run.

## Not done
No campaign API, so campaign results cannot be browsed in the dashboard.
npm audit upgrades (vite/vitest majors; dev-only, test in a branch), feature decision report, design system and light/dark theme, page redesign, Research section, documentation rewrite.

## Review pass 3: ML and Research removed (owner decision)
The prediction module, its model bundle, the `/models` and `/predictions` API routes, the `predict` request fields, the Research navigation group and the Models, Predictions and Campaign analysis pages were removed. The scikit-learn pin above no longer applies (scikit-learn and joblib are no longer dependencies; scipy is now declared directly because the statistics code uses it). Campaign and statistics command-line tools remain, without prediction columns. Backend: only the sandbox-only root test fails. Frontend: 96 passed, 7 skipped; build passes (bundle 237 kB to 219 kB). Verified in headless Chromium against the real API: all five approaches ran, compare, history, theme and mobile menu worked, `predict` is rejected as an unknown field and `/api/models` returns 404.
