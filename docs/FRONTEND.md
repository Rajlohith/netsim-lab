# React dashboard

A React 18 + JavaScript (Vite) single-page app in `frontend/`. It talks only to the FastAPI endpoints (`docs/API.md`) and adds **no
dependency** beyond the ones the project already declared: routing is a 40-line hash router, charts are hand-written responsive SVG.

```
make frontend-install   # once (needs network and Node.js 20.19+)
make stack-start        # API + dashboard in the background, or:
make run-api            #   terminal 1: the API
make frontend-dev       #   terminal 2: http://localhost:5173  (Vite proxies /api -> 127.0.0.1:8000)
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
| Overview (`overview`, the default page) | `GET /experiments` (recent, totals), `approaches`, `scenarios`, `GET /api/topology` | Introduction, three counts read from the API, topology preview and recent experiments. Nothing is computed in the browser |
| Network topology (`topology`) | `GET /api/topology` *(new, read-only)* | SVG diagram + tables of nodes, interfaces, links, configured netem values, routes. The endpoint parses `config/topology_ro.yaml` (the file the experiments use) with the topology loader |
| Configure experiment (`configure`) | `approaches`, `scenarios`, `capabilities`, `POST /experiments/validate`, `POST /experiments`, `POST …/cancel`, `GET /experiments?status=queued&status=running` | approach (unrunnable ones disabled with the server's reason), scenario, environment, handoff mode, traffic profile (duration, rate, payload, seed), condition overrides, optional predictions, label. Client validation uses the server's limits; the server's field errors are shown on the fields. Start uses an `Idempotency-Key` (double click = one experiment). Stop needs a second confirming click |
| Experiment results (`live`, `live/<id>`) | `GET /experiments/{id}`, `…/transitions`, then `…/metrics`, `…/handoffs`, `…/events` | **Polling every second** (`capabilities.live_updates.websocket` is false). Shows phase, lifecycle log, errors, stop control; after completion KPIs (RTT, loss, goodput, overhead, handoffs, interruption), per-handoff bar charts, an event timeline and exports |
| Approach comparison (`compare`) | `GET /experiments`, `GET /experiments/{id}`, `…/metrics` | 1–6 completed experiments; conditions table; a warning listing every condition that differs; metric table; six charts (interruption/service gap, loss, latency, overhead, signalling, goodput); CSV/JSON export of the displayed rows |
| Experiment history (`history`, `history/<id>`) | `GET /experiments` (filter/page), `GET /experiments/{id}`, `…/transitions`, exports | configuration, status, error, lifecycle; export JSON / CSV (handoffs, events) / ZIP, partial-output ZIP for failed runs; list export (CSV/JSON) honouring filters |

## What is real, simulated and "live"

* **Nothing is live.** The backend runs experiments in the simulator's virtual time (or on loopback sockets) and writes metrics when a run ends.
  The Live page therefore polls the *lifecycle* and loads measurements after completion; it says so on the page and never animates data.
* Every result carries an origin badge ("Simulated · historical · not live"). Times from events are on the **run's own clock**
  (labelled "run clock"); wall-clock timestamps are UTC.
* `null` (not measurable) is displayed as "—" / "n/a", never as 0 — e.g. interruption time for approaches that cannot measure it.
* **Jitter is not shown as a value:** the comparison metric set has no jitter series. The KPI says "not reported".
* The progress bar appears only if the runner reports a fraction (it reports phases only), so no percentage is ever invented.
* Charts and exports share one code path: comparison rows → table, charts and CSV/JSON (`src/lib/metrics.js`); the per-experiment exports are the
  server's files, and `src/integration/live-api.test.js` checks that the JSON export equals the document the charts use and the CSV rows equal the
  plotted per-handoff series.

## Testing

| Command | What it covers |
|---|---|
| `make frontend-test` | vitest: logic, API client, server-rendered views and jsdom interaction tests |
| `make frontend-integration` | the dashboard's API layer against the real service layer through a small stdlib HTTP adapter (`scripts/service_http_adapter.py`) |
| `make e2e-browser` | real headless Chrome against the real service layer: start, cancel and export experiments, compare on-screen values with the API |

Fixtures in `src/test/fixtures/api/` are real API responses recorded from simulator runs. They are test inputs, not demo data: the app itself ships none.

## Known limitations

* No WebSocket / no live metrics (backend limitation, stated above); no jitter series in comparison metrics.
* Comparison compares single runs; no confidence intervals (replicated statistics come from the command-line campaigns and are not exposed through the API).
* Scenario handoff schedules (moves) are shown but not editable. QUIC runs only if `aioquic` is installed on the server (the form follows `/api/approaches`).
* Event timeline loads at most 5000 events (the API's own limits apply).
* No bearer-token entry UI; no experiment deletion UI (the API has none).
* Colour is never the only carrier (values are printed, every chart has a data table) but a full accessibility audit was not done.


## Theme and layout

Two navigation groups (Workspace, Experiments) in a dark sidebar; on screens under 860 px it becomes a menu. Every colour is a CSS token in `src/styles.css`; the theme is `data-theme` on `<html>`, set before the first paint by a small script in `index.html`, toggled from the header and stored in `localStorage` (key `netsim-theme`; blocked storage falls back to the operating-system preference). Charts and the topology diagram use the same tokens.
