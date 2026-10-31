# STAGE 9 MANIFEST — React dashboard (`netsim-lab-stage-9.zip`)

Complete project (Stages 1–9). Top-level folder inside the archive: `netsim-lab/`. File list with sha256: `STAGE9_MANIFEST.json`.

## What Stage 9 added
* `frontend/` — dashboard: 7 pages, API client layer, SVG charts, hash router, tests (`docs/FRONTEND.md`).
* Backend (additive, read-only): `GET /api/topology` (`backend/app/services/topology.py`, route in `api/routes/catalog.py`, schema `TopologyOut`, `docs/API.md`), tests `backend/tests/test_api_topology.py` (+1 HTTP test in `test_api_http.py`, skipped without fastapi). No existing behaviour was changed.
* `scripts/service_http_adapter.py` (**verification harness only**, stdlib HTTP over the real service layer), `scripts/capture_frontend_fixtures.py` (captures 37 real API responses from 4 real runs), `scripts/make_stage9_zip.py`.

## Setup
```
make setup                         # backend venv + deps (see README)
make run-api                       # FastAPI on 127.0.0.1:8000
make frontend-install              # npm install (needs network); then commit frontend/package-lock.json (none shipped)
make frontend-dev                  # http://localhost:5173, proxies /api
make frontend-test                 # vitest run   (unit + SSR + jsdom interaction + skipped-by-default integration)
make frontend-build                # vite build
NETSIM_API_URL=http://127.0.0.1:8000/api npm --prefix frontend run test:integration   # against the real FastAPI app
make api-test                      # Stage 8 + topology HTTP tests (needs fastapi, httpx)
# no-network substitutes used to build this stage:
make frontend-verify               # esbuild bundle + logic/SSR tests with a vitest shim
make frontend-integration          # dashboard API layer vs the real service layer (stdlib adapter)
```

## Test results (ACTUAL runs in the build environment; Python 3.12, Node 22.22, React 19.2.5 (project declares ^18.3.1), no network)
| Suite | Result |
|---|---|
| Backend, all modules (`python scripts/run_all_tests.py`, written to `docs/TEST_REPORT.md`) | **551 ran, 0 failures, 0 errors, 25 skipped, 526 passed**; 51 modules; `test_api` and `test_config` NOT RUNNABLE (missing fastapi/pydantic) — i.e. every HTTP-level API test, including the new topology one, did **not** run |
| New backend module `test_api_topology` | 5 ran, 5 passed |
| Frontend logic + API client + SSR view tests (`run-sandbox-tests.mjs`, vitest-shim, **not vitest**) | **90 passed, 0 failed** (App 1, api 10, lib 14, logic 26, views 39) |
| Frontend API integration vs real service layer (`run-integration.sh`) | **7 passed, 0 failed** (+ the 90 above = 97 in that run) |
| Frontend production bundle (esbuild, **not `vite build`**) | OK — main.js 276.5 kB, main.css 7.4 kB, 42 modules, no unresolved imports |
| `frontend/src/interaction.test.jsx` (RTL + jsdom; navigation clicks, start/stop, polling, exports, filters) | **NOT RUN** (no jsdom/@testing-library); esbuild syntax check only |
| `npm test` (vitest), `npm run build` (vite), real FastAPI HTTP routes, real-browser rendering | **NOT RUN / UNVERIFIED** |

## Known limitations
See `docs/FRONTEND.md` ("Verification status", "Known limitations"). Key points: nothing is live (backend writes metrics when a run ends; the dashboard polls the
lifecycle); no jitter in the comparison metrics; single-run comparisons without confidence intervals; no token-entry UI; no `package-lock.json`;
layout/CSS never viewed in a browser; the interaction tests were never executed and may need small fixes on first run.

## Project structure (frontend; backend unchanged apart from the topology endpoint)
```
frontend
frontend/index.html
frontend/package.json
frontend/scripts
frontend/scripts/bundle-check.mjs
frontend/scripts/run-integration.sh
frontend/scripts/run-sandbox-tests.mjs
frontend/src
frontend/src/App.jsx
frontend/src/App.test.jsx
frontend/src/api
frontend/src/api/api.test.js
frontend/src/api/client.js
frontend/src/api/endpoints.js
frontend/src/components
frontend/src/components/Badges.jsx
frontend/src/components/Common.jsx
frontend/src/components/StateViews.jsx
frontend/src/components/charts
frontend/src/components/charts/BarChart.jsx
frontend/src/components/charts/ChartTable.jsx
frontend/src/components/charts/LineChart.jsx
frontend/src/components/charts/TimelineChart.jsx
frontend/src/hooks
frontend/src/hooks/api.jsx
frontend/src/hooks/useAsync.js
frontend/src/hooks/usePolledExperiment.js
frontend/src/integration
frontend/src/integration/live-api.test.js
frontend/src/interaction.test.jsx
frontend/src/lib
frontend/src/lib/csv.js
frontend/src/lib/download.js
frontend/src/lib/experimentActions.js
frontend/src/lib/format.js
frontend/src/lib/lib.test.js
frontend/src/lib/logic.test.js
frontend/src/lib/metrics.js
frontend/src/lib/numbers.js
frontend/src/lib/poll.js
frontend/src/lib/router.js
frontend/src/lib/scale.js
frontend/src/lib/validation.js
frontend/src/main.jsx
frontend/src/pages
frontend/src/pages/ComparePage.jsx
frontend/src/pages/ConfigurePage.jsx
frontend/src/pages/HistoryPage.jsx
frontend/src/pages/LivePage.jsx
frontend/src/pages/ModelsPage.jsx
frontend/src/pages/PredictionsPage.jsx
frontend/src/pages/TopologyPage.jsx
frontend/src/pages/views.test.jsx
frontend/src/styles.css
frontend/src/test
frontend/src/test/fixtures
frontend/src/test/fixtures/api
frontend/src/test/mockApi.js
frontend/test-support
frontend/test-support/vitest-shim.mjs
frontend/vite.config.js
```
Top-level: `backend/ config/ docs/ examples/ frontend/ schemas/ scripts/ data/ Makefile README.md PROJECT_STATUS.md NEXT_STEPS.md requirements*.txt STAGE{6,7,8,9}_MANIFEST.*`
