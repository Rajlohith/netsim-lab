# Stage 9 — React dashboard

A React 18 + JavaScript (Vite) single-page app in `frontend/`. It talks only to the Stage 8 FastAPI endpoints (`docs/API.md`) and adds **no
dependency** beyond the ones the project already declared: routing is a 40-line hash router, charts are hand-written responsive SVG.

```
make stack-start          # or: make run-api (API only, with --reload) in terminal 1
make frontend-install   # once (needs network)
make frontend-dev       # terminal 2: http://localhost:5173  (Vite proxies /api -> 127.0.0.1:8000)
make frontend-test      # vitest (unit + SSR views + jsdom interactions)
make frontend-build     # production build
```
`VITE_API_BASE_URL` points the build at another origin (the API must then allow CORS). The dashboard has **no UI for the optional bearer token**
(`NETSIM_API_TOKEN`); with a token configured the API client supports it (`createClient({ token })`) but no page lets you enter one.

## Architecture

| Layer | Files | Rule |
|---|---|---|
| HTTP | `src/api/client.js`, `src/api/endpoints.js` | the only code that calls `fetch`; every failure becomes an `ApiError` (network errors included) |
| Logic | `src/lib/*` | no React: validation (limits read from `/api/capabilities`), start/stop actions, polling controller, metric extraction, CSV, formatting, router |
| State | `src/hooks/*` | `useAsync`, `usePolledExperiment`, `ApiProvider` (pages receive the API object from context, so tests inject their own) |
| Presentation | `src/components/*`, `src/pages/*` | each page = container (hooks) + pure view (props only) |

## Pages and the data behind them

| Page (`#/…`) | Endpoints | Notes |
|---|---|---|
| Network topology (`topology`) | `GET /api/topology` *(new, read-only)* | SVG diagram + tables of nodes, interfaces, links, configured netem values, routes. The endpoint parses `config/topology_ro.yaml` (the file the experiments use) with the Stage 2 loader |
| Experiment configuration (`configure`) | `approaches`, `scenarios`, `capabilities`, `models`, `POST /experiments/validate`, `POST /experiments`, `POST …/cancel`, `GET /experiments?status=queued&status=running` | approach (unrunnable ones disabled with the server's reason), scenario, environment, handoff mode, traffic profile (duration, rate, payload, seed), condition overrides, optional predictions, label. Client validation uses the server's limits; the server's field errors are shown on the fields. Start uses an `Idempotency-Key` (double click = one experiment). Stop needs a second confirming click |
| Live metrics (`live`, `live/<id>`) | `GET /experiments/{id}`, `…/transitions`, then `…/metrics`, `…/handoffs`, `…/events` | **Polling every second** (`capabilities.live_updates.websocket` is false). Shows phase, lifecycle log, errors, stop control; after completion KPIs (RTT, loss, goodput, overhead, handoffs, interruption), per-handoff bar charts, an event timeline and exports |
| Handoff prediction (`predictions`, `predictions/<id>`) | `GET /experiments?status=completed`, `GET …/predictions` | model id/algorithm/**version**, threshold, score-over-run-time chart with alarms and handoff onsets, paged decision-point table (decision time, horizon, score, alarm, last observation), "run in the model's training split" flag |
| Approach comparison (`compare`) | `GET /experiments`, `GET /experiments/{id}`, `…/metrics` | 1–6 completed experiments; conditions table; a warning listing every condition that differs; metric table; six charts (interruption/service gap, loss, latency, overhead, signalling, goodput); CSV/JSON export of the displayed rows |
| Model evaluation (`models`) | `GET /models`, `GET /models/{id}/evaluation` | baseline / logistic regression / Random Forest on validation and test, always-alarm reference, bootstrap intervals, class balance per split, caveats |
| Experiment history (`history`, `history/<id>`) | `GET /experiments` (filter/page), `GET /experiments/{id}`, `…/transitions`, exports | configuration, status, error, lifecycle; export JSON / CSV (handoffs, events, predictions) / ZIP, partial-output ZIP for failed runs; list export (CSV/JSON) honouring filters |

## What is real, simulated and "live"

* **Nothing is live.** The backend runs experiments in the simulator's virtual time (or on loopback sockets) and writes metrics when a run ends.
  The Live page therefore polls the *lifecycle* and loads measurements after completion; it says so on the page and never animates data.
* Every result carries an origin badge ("Simulated · historical · not live"). Times from events and predictions are on the **run's own clock**
  (labelled "run clock"); wall-clock timestamps are UTC.
* `null` (not measurable) is displayed as "—" / "n/a", never as 0 — e.g. interruption time for approaches that cannot measure it.
* **Jitter is not shown as a value:** the comparison metric set has no jitter series. The KPI says "not reported".
* The progress bar appears only if the runner reports a fraction (it reports phases only), so no percentage is ever invented.
* Charts and exports share one code path: comparison rows → table, charts and CSV/JSON (`src/lib/metrics.js`); the per-experiment exports are the
  server's files, and `src/integration/live-api.test.js` checks that the JSON export equals the document the charts use and the CSV rows equal the
  plotted per-handoff series.

## Verification status (what was actually run — see also STAGE9_MANIFEST.md)

The build environment had **no network access**, so `npm install` was impossible: no vite, vitest, jsdom, or @testing-library, and no FastAPI.
Therefore:

| Item | Status |
|---|---|
| `npm test` (vitest) | **not run** here. The suite is written for vitest |
| Logic, API-client and server-side-rendering tests (90 tests) | **run and passing** with `scripts/run-sandbox-tests.mjs`: esbuild + a small vitest-API shim (`test-support/vitest-shim.mjs`), React 19.2 rendering (project declares ^18.3.1) |
| `src/interaction.test.jsx` (RTL + jsdom: navigation clicks, start/stop, polling to completion, export clicks, filters) | written, syntax-checked only — **not run** |
| `npm run build` (vite) | **not run**. Replaced by `scripts/bundle-check.mjs` (esbuild production bundle: 276 kB JS + 7 kB CSS, no unresolved imports) |
| API integration | `src/integration/live-api.test.js` ran (7 tests) against the real `ExperimentService` through `scripts/service_http_adapter.py`, a stdlib HTTP shim. It validates the dashboard's client against genuine service output and a real experiment run; it does **not** exercise FastAPI routing, pydantic parsing, CORS or auth |
| `GET /api/topology` HTTP route | route and schema only byte-compiled (syntax), never executed; service function tested (`tests/test_api_topology.py`); the HTTP route itself needs `make api-test` with fastapi installed |
| Real browser rendering | **Stage 10: partly verified.** `scripts/e2e_browser.py` drives headless Chrome 131 at 1280x900 against the real service layer (31 checks passed, `docs/e2e_browser_results.json`, screenshots in `docs/demo/`). Not checked: other viewport widths, dark mode, focus order, Firefox/Safari. The same run found and fixed two display bugs (1) event-timeline caption showed `0.000 s – 0.000 s` (unit mix-up; regression test added), (2) the top bar stretched to ~300 px on short pages (CSS grid rows; fixed) |

Fixtures in `src/test/fixtures/api/` are real responses captured by `scripts/capture_frontend_fixtures.py` from four real simulator runs
(`make frontend-fixtures` regenerates them). They are test inputs, not demo data: the app itself ships none.

## Known limitations

* No WebSocket / no live metrics (backend limitation, stated above); no jitter series in comparison metrics.
* Comparison compares single runs; no confidence intervals (replicated statistics live in Stage 7 outputs, not exposed through the API).
* Scenario handoff schedules (moves) are shown but not editable. QUIC runs only if `aioquic` is installed on the server (the form follows `/api/approaches`).
* Event timeline loads at most 5000 events; predictions at most 5000 decision points per view (the API's own limits apply).
* No bearer-token entry UI; no experiment deletion UI (the API has none).
* No `package-lock.json`: it cannot be generated without network. Run `npm install` once and commit it.
* Colour is never the only carrier (values are printed, every chart has a data table) but a full accessibility audit was not done.
