# Stage 8 — Backend API

Managed experiments over HTTP: validate a configuration, start it as a **background job**, watch its lifecycle, read metrics / handoff events /
handoff predictions **after it completes**, export results, browse history. The API runs the **existing** experiment code
(`app.compare.run_experiment`, `app.ml.integration.annotate_run`); it adds orchestration and persistence, never measurements.

* Base path `/api`. Interactive docs at `/docs` (Swagger) and `/openapi.json` when the server runs. `make api-openapi` writes
  `schemas/api/openapi.json` from the code (needs FastAPI installed — see *Verification status* in `STAGE8_MANIFEST.md`).
* Start: `make run-api` (unprivileged, `127.0.0.1:8000`). The job manager starts with the server (FastAPI lifespan).
* No FastAPI available? `make api-smoke` runs one real experiment through the same service layer without HTTP.

## 1. Design decisions

| Topic | Decision | Why |
|---|---|---|
| Job execution | Every experiment runs in its **own worker process** (`python -m app.services.worker <job dir>`), started by a scheduler thread; request handlers only validate and enqueue | long jobs never block or crash the server; a process can be killed, a thread cannot |
| Live updates | **Polling, no WebSocket** | Runs execute in the simulator's virtual time and produce their metrics only when they finish. There is no metric stream to forward; a WebSocket would have nothing true to send. `GET /api/capabilities` → `live_updates` states this machine-readably. Poll `GET /api/experiments/{id}` or, for incremental updates, `GET /api/experiments/{id}/transitions?after_seq=N` |
| Progress | phase only (`queued → starting → running_experiment → predicting → finalizing → done`); `fraction` is `null` while running | the runner has no finer progress; inventing a percentage would be fabrication |
| Metadata | SQLite (`data/api/experiments.sqlite3`, WAL); raw outputs are files in `data/api/experiments/<id>/` | metadata is small and relational; measurements stay in the formats of earlier stages |
| Data origin | every result carries `provenance` (`data_origin`, `is_live: false`) | results are simulator-generated (or real loopback TCP), read from files after the run |
| Privileges | the API never starts privileged work: the `live` environment is **not selectable**; the launcher uses a fixed argv, no shell, no sudo; the manager refuses to run as root | project constraint: never run the app as root |
| Prediction | computed **in the worker** right after the run when the request has `"predict": true`; served from the files the Stage 6 code wrote | no model is unpickled in the API process |

## 2. Lifecycle

```
            +--------> cancelled   (cancel while queued: immediate)
 queued ----+--------> failed      (worker could not be launched)
    |
    v
 running ---+--------> completed   worker reported ok and comparison_metrics.json is readable
            +--------> failed      experiment/runner reported an error, or timeout (error.code = timeout)
            +--------> cancelled   cancel requested: SIGTERM to the worker's process group, SIGKILL after the grace period
            +--------> incomplete  worker ended without a result (crash / killed from outside / OOM), or the server restarted/stopped
```
Terminal states are final (compare-and-set in SQLite; every change is appended to `transitions`).

| Rule | Behaviour |
|---|---|
| Duplicate runs | At most one **active** (queued/running) experiment per configuration fingerprint (hash of the fully resolved spec + prediction options; `label` is not part of it). Enforced by a partial `UNIQUE` index, not only in code. A second identical request → `409 duplicate_active_experiment` naming the first. Once it finished, the same configuration may run again |
| Idempotency | `Idempotency-Key: <1-128 chars of A-Za-z0-9._:->` — repeating the same body returns the original experiment (`200`, `Idempotent-Replayed: true`); same key with a different body → `409 idempotency_key_reused` |
| Concurrency | `NETSIM_API_MAX_CONCURRENT_JOBS` (default 2) workers; FIFO queue of `NETSIM_API_MAX_QUEUE` (default 20), beyond which `429 queue_full` + `Retry-After` |
| Cancel race | A cancel that arrives after the worker finished does not undo it: the status is `completed` and `cancel_requested` stays `true` |
| Cancelled outputs | the half-written run directory is **deleted** (`partial_outputs: false`) |
| Failed/incomplete outputs | partial files are **kept** for diagnosis, flagged `partial_outputs: true`, never served by `/metrics`, `/handoffs`, `/events`, `/predictions`, JSON/CSV export. Only `export?format=zip&include_partial=true` returns them, with `partial: true` in the manifest |
| Restart | at start-up: `running` rows → `incomplete` (`server_restarted`; or `cancelled` if a cancel was pending; or `completed` if the worker had already written a good result). A surviving orphan worker is killed only if pid **and** start time **and** command line match. `queued` rows stay queued and run |
| Clean shutdown | running workers are terminated and become `incomplete` (`server_shutdown`); queued ones stay queued |
| Single owner | an `flock` on `data/api/server.lock` allows one API process per data directory (the second refuses to start) |
| Timeout | `NETSIM_API_MAX_RUNTIME_S` (300 s) wall clock per job, plus `RLIMIT_CPU`/`RLIMIT_FSIZE` inside the worker |
| Retention | finished experiments older than `NETSIM_API_RETENTION_DAYS` (30; 0 = forever) are deleted at start-up and hourly, together with orphan directories older than 1 h that have no database row. `DELETE /api/experiments/{id}` removes one finished experiment |

## 3. Endpoints

All responses are JSON unless noted. `{id}` has the form `exp-` + 16 hex digits; anything else is `404 not_found`.

| Method & path | Purpose | Success | Errors |
|---|---|---|---|
| `GET /api/health` | liveness (Stage 1) | 200 | |
| `GET /api/approaches` | approaches, what they are/aren't, where they can run **on this server** | 200 | 401 |
| `GET /api/scenarios` | network scenarios with every resolved parameter | 200 | 401 |
| `GET /api/topology` | the lab topology the experiments run on: nodes, links, routes and the *configured* netem values (read from `config/topology_ro.yaml` with the Stage 2 loader; added in Stage 9 for the dashboard) | 200 | 401 |
| `GET /api/capabilities` | limits, accepted fields, live-update policy | 200 | 401 |
| `GET /api/status` | job-manager state, counts per status | 200 | 401 |
| `POST /api/experiments/validate` | check a request without running it | 200 (`valid` true/false) | 401, 422 (malformed body) |
| `POST /api/experiments` | validate + queue | **202** (`Location` header); 200 on idempotent replay | 401, 409, 422, 429, 503 |
| `GET /api/experiments` | history: filter `status`(repeatable) `approach` `scenario` `environment` `created_after` `created_before` `label`; `sort` (`-created_at` default); `limit` (1–500, 50) `offset` | 200 `{items,total,limit,offset}` | 401, 422 |
| `GET /api/experiments/{id}` | status, configuration, progress, error, summary, prediction status, links | 200 | 401, 404 |
| `GET /api/experiments/{id}/transitions?after_seq=N&limit=` | append-only lifecycle log for incremental polling | 200 | 401, 404 |
| `POST /api/experiments/{id}/cancel` | cancel queued (200) / running (**202**, poll) | 200/202 | 401, 404, 409 `not_cancellable`, 503 |
| `DELETE /api/experiments/{id}` | delete a finished experiment and its files | 204 | 401, 404, 409 `experiment_active`, 503 |
| `GET /api/experiments/{id}/metrics` | the run's `comparison_metrics.json` + provenance | 200 | 401, 404, 409 `results_not_available` / `results_missing` |
| `GET /api/experiments/{id}/handoffs` | per-handoff metrics + summary | 200 | same |
| `GET /api/experiments/{id}/events?after_seq=&limit=` | raw approach event log, paginated by `seq` | 200 | same + 409 `events_not_available` |
| `GET /api/experiments/{id}/predictions?limit=&offset=` | Stage 6 predictions for this run | 200 | same + 404 `predictions_not_available` |
| `GET /api/experiments/{id}/export?format=json\|csv\|zip` | export (`csv`: `table=handoffs\|events\|predictions`; `zip`: `include_partial=true`) | 200 file | 401, 404, 409, 422 |
| `GET /api/history/export?format=csv\|json` | one summary row per experiment (same filters as the list, minus label/sort/paging; max 10 000 rows) | 200 file | 401, 422 |
| `GET /api/models` | trained prediction bundles (verified against their MANIFEST) | 200 | 401 |
| `GET /api/models/{model_id}/evaluation` | held-out evaluation recorded at training time + caveats | 200 | 401, 404 |

Authentication: none by default (the server binds to 127.0.0.1). With `NETSIM_API_TOKEN` set, every route except `/api/health` needs
`Authorization: Bearer <token>` (`401 unauthorized`, `WWW-Authenticate: Bearer`).

## 4. Request schema — `POST /api/experiments` and `/validate`

Unknown fields are **rejected** (`unknown_field`), never ignored. Not accepted at all: topology paths, commands, run/scenario ids, link names,
`link_semantics` — the server fixes them. Explicit fields override the chosen `scenario`.

| Field | Type | Default | Limits / meaning |
|---|---|---|---|
| `approach` | string | **required** | one of `GET /api/approaches` (`tcp-reconnect`, `mip-basic`, `mip-mbb`, `ro-model`, `quic-migration`) |
| `scenario` | string | `baseline` | one of `GET /api/scenarios` |
| `environment` | string | `emulated` | `emulated` or `loopback` (loopback only for `tcp-reconnect`, `quic-migration` where the registry says so); `live` → `environment_not_exposed` |
| `handoff_mode` | string | `bbm` | `bbm` or `mbb` (`mbb` only for approaches that support it) |
| `duration_s` | number | scenario / 10 | (0, 120] |
| `rate_pps` | number | 100 | (0, 1000]; `duration_s × rate_pps ≤ 120 000` |
| `payload` | integer | 64 | 36–1472 |
| `seed` | integer | 0 | 0–2³¹−1 |
| `initial_iface` | string | `eth-a` | interface name, validated against the topology |
| `moves` | `[{at_s, interface}]` | scenario / `[{4.0,eth-b},{7.0,eth-a}]` | 1–20 entries, times inside the run, interfaces from the topology |
| `tcp_detect` | string | `event` | `event` or `timeout` (tcp-reconnect) |
| `old_link_hold_ms`, `detect_delay_ms` | number | 100, 20 / scenario | 0–10 000 |
| `conditions` | object | scenario | `latency_scale`, `loss_percent`, `ha_distance_ms`, `registration_loss_percent`, `ha_processing_ms` (ranges as in `app.netem.conditions`) |
| `predict` | boolean | false | also compute handoff predictions |
| `prediction_model` / `prediction_algorithm` | string | `stage6` / `random_forest` | only with `predict: true` |
| `label` | string | – | ≤ 80 printable characters |

Limits are the defaults of `ServiceSettings` and can be changed through `NETSIM_API_*` (see `.env.example`). The runner's own sanity checks
(`app.compare.runner.check`) run on top: unsupported combination → `unsupported_combination`, missing dependency (e.g. `aioquic`) →
`approach_unavailable`, impossible parameters (e.g. a move outside the run) → `invalid_parameter`.
A setting that cannot act on the chosen approach (e.g. `ha_distance_ms` for `tcp-reconnect`) is a **warning** (`condition_has_no_effect`), not an error.

## 5. Responses

**Error envelope** (every non-2xx, including unknown routes and framework validation errors):
```json
{"error": {"code": "validation_failed", "message": "the experiment request is not valid", "request_id": "5d0c…",
           "issues": [{"field": "duration_s", "code": "out_of_range", "message": "duration_s must be in (0, 120.0], got 500"}],
           "context": {}}}
```
`issues` = per-field problems (`required`, `unknown_field`, `invalid_type`, `out_of_range`, `unknown_approach`, `unknown_scenario`, `invalid_choice`,
`environment_not_exposed`, `unsupported_combination`, `approach_unavailable`, `invalid_parameter`, `probe_budget_exceeded`, `too_many_moves`,
`model_unavailable`, `inconsistent`, `invalid_body`, `invalid_json`); `context` = extra facts (e.g. `existing_experiment_id`). The `X-Request-ID` header
(echoed if the client sends a safe one) equals `request_id`.
Codes by status: 401 `unauthorized` · 404 `not_found`, `predictions_not_available`, `model_unavailable` · 405 `method_not_allowed` ·
409 `duplicate_active_experiment`, `idempotency_key_reused`, `not_cancellable`, `experiment_active`, `results_not_available`, `results_missing`, `events_not_available` ·
422 `validation_failed` · 429 `queue_full` · 500 `internal_error` · 503 `service_unavailable`.

**Experiment object** (`GET /api/experiments/{id}`; from a real run):
```json
{"id": "exp-9392ce8cb7319ce9", "status": "completed", "approach": "mip-mbb", "scenario": "lossy-access", "environment": "emulated",
 "label": "mbb under 2% loss", "created_at": "2026-10-29T20:51:54.387965Z", "started_at": "…", "finished_at": "…",
 "configuration": {"…every effective parameter…": 0}, "resolved_spec": {"…ExperimentSpec as stored…": 0},
 "progress": {"phase": "done", "step": null, "steps_total": null, "fraction": 1.0, "queue_position": null, "note": null},
 "cancel_requested": false, "error": null, "partial_outputs": false,
 "result": {"available": true, "summary": {"handoff_count": 2, "all_handoffs_recovered": true, "handoff_latency_ms_mean": 71.0, "handoff_latency_ms_max": 91.0,
            "service_gap_ms_mean": 25.0, "loss_packets_total": 1, "loss_percent": 1.894737, "packets_lost": 18, "packets_sent": 950, "loss_is_exact": true,
            "rtt_p50_ms": 33.0, "goodput_bps": 50228.872, "mobility_overhead_bytes_per_packet": 20.0, "run_status": "complete", "data_origin": "simulated"}},
 "prediction": {"requested": true, "status": "available", "model": "stage6", "algorithm": "random_forest", "error": null},
 "data": {"origin": "simulated", "is_live": false, "live_metrics_available": false, "note": "…"},
 "links": {"self": "/api/experiments/exp-…", "cancel": "…", "transitions": "…", "metrics": "…", "handoffs": "…", "events": "…", "predictions": "…", "export": "…"}}
```
`POST` additionally returns `warnings`. While `queued`, `progress` = `{"phase":"queued","queue_position":N,…}`; while `running`, `{"phase":"running_experiment"|"predicting"|"finalizing","step":k,"steps_total":n,"fraction":null}`.
`error` is `{code, message}` for `failed`/`incomplete` (`launch_failed`, `timeout`, `worker_died`, `server_restarted`, `server_shutdown`, `results_unreadable`, or the runner's own outcome: `failed`, `invalid`, `unsupported`, `unavailable`).
`prediction.status`: `not_requested`, `pending`, `available`, `failed` (the experiment is still `completed`; `error` says why), `not_run` (the experiment did not complete).

**Results** (`metrics`, `handoffs`, `events`, `predictions`) all start with `{experiment_id, status, approach, scenario, environment, provenance}`;
`provenance = {data_origin: "simulated"|"real-kernel-loopback…", is_live: false, clock, network, note}`.
* `metrics.metrics` is the run's `comparison_metrics.json` unchanged (schema: `schemas/`, `docs/MOBILITY_COMPARISON.md`). `null` means *not measurable*, never zero.
* `handoffs.handoffs[]`: `event_id, detail, recovered, handoff_latency_ms, service_gap_ms, interruption_ms, loss_packets, rtt_before_ms, rtt_after_ms, signalling_messages, signalling_bytes, retransmits, failures[]`, plus `summary` and `unavailable_reason`.
* `events.events[]`: `seq, mono_ns, wall_ns, approach, node, kind, bytes, detail`. `mono_ns`/`wall_ns` are the **run's own clock** (virtual for emulated runs — not wall-clock time of the request). `next_after_seq` is `null` on the last page.
* `predictions`: `model {id, algorithm, version}`, `summary` (= the run's `handoff_predictions.json`: alarms, ground-truth episodes, `run_in_training_split`, provenance note), `decision_points {items[], total, limit, offset}` where each item has `decision_time_ns, horizon_end_ns, horizon_s, score, alarm, threshold, last_observation_ns`, and `caveat`.
* `GET /api/models/{id}/evaluation`: `held_out_test_metrics` per algorithm, `reference` (always-alarm), `full_results.findings` when `results.json` matches the bundle's dataset hash, and an `interpretation_note`: **the shipped model was trained on simulated data with abrupt handoffs and no gradual degradation; its recorded results do not show skill beyond the always-alarm reference** (see `docs/HANDOFF_PREDICTION.md`).

**Exports.** `json`: `{export_schema_version, generated_at, is_live, experiment, provenance, metrics, predictions}`. `csv`: one table (`handoffs`, `events`, `predictions`); `null` → empty cell; text cells starting with `= + - @ TAB CR` get a leading `'` (spreadsheet formula injection; `label` is client text). `zip`: `run/<raw files>` + `experiment_record.json` + `EXPORT_MANIFEST.json` (sha256 per member, `partial`, `is_live`, `data_origin`). History export: `experiment_id, status, approach, scenario, environment, label, timestamps, error_code, <summary values>, prediction_status`.

## 6. Example requests

```bash
BASE=http://127.0.0.1:8000/api
curl -s $BASE/approaches | jq '.approaches[] | {id, runnable: .environments.emulated.runnable_here}'
curl -s $BASE/scenarios  | jq '.scenarios[].id'

# dry run
curl -s -X POST $BASE/experiments/validate -H 'content-type: application/json' \
     -d '{"approach":"mip-basic","environment":"live"}' | jq '.valid, .issues'

# start (202 + Location), then poll
ID=$(curl -s -X POST $BASE/experiments -H 'content-type: application/json' -H 'Idempotency-Key: demo-1' \
     -d '{"approach":"mip-mbb","handoff_mode":"mbb","scenario":"lossy-access","seed":7,"predict":true,"label":"mbb 2% loss"}' | jq -r .id)
until [ "$(curl -s $BASE/experiments/$ID | jq -r .status)" != "queued" ] && [ "$(curl -s $BASE/experiments/$ID | jq -r .status)" != "running" ]; do sleep 1; done
curl -s $BASE/experiments/$ID/transitions | jq '.items[] | [.seq,.to_status,.note]'

# results (only after completed)
curl -s $BASE/experiments/$ID/handoffs     | jq '.handoffs[0]'
curl -s "$BASE/experiments/$ID/events?limit=5"
curl -s "$BASE/experiments/$ID/predictions?limit=3" | jq '.summary.alarms, .caveat'
curl -s $BASE/models/stage6/evaluation | jq '.interpretation_note'

# export
curl -s -OJ "$BASE/experiments/$ID/export?format=zip"
curl -s "$BASE/experiments/$ID/export?format=csv&table=handoffs"
curl -s "$BASE/history/export?format=csv&status=completed" > history.csv

# cancel / delete
curl -s -X POST   $BASE/experiments/$ID/cancel
curl -s -X DELETE $BASE/experiments/$ID -o /dev/null -w '%{http_code}\n'
# with a token configured:  -H "Authorization: Bearer $NETSIM_API_TOKEN"
```

## 7. Safety summary

* No client-supplied command, path, topology file, link name, run id or scenario id exists in the API; the worker takes its only input from a server-written `job.json`.
* The privileged `live` environment cannot be requested (validation), enabled (settings reject it) or run (the worker refuses it again).
* The only process the API starts is the worker: fixed argv, `shell=False`, own session/process group, filtered environment (no token), `RLIMIT_FSIZE`/`RLIMIT_CPU`; signals go to that group only (never to the server's own group or pid ≤ 1). `tests/test_project_structure.py` pins this.
* The manager refuses to run as root unless `NETSIM_API_ALLOW_ROOT=1` (disposable containers only).
* Paths: ids are validated (`^exp-[0-9a-f]{16}$`) and every resolved path must stay inside the experiments directory; ZIP exports include only top-level `.csv/.json/.txt` regular files of the run directory.
* Not provided: user accounts, rate limiting per client, TLS (put a reverse proxy in front before exposing the API beyond localhost), multi-process/multi-host job execution.
