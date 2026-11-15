# NetSim Lab: features, combinations and how everything works

This guide explains every feature of NetSim Lab, how it works inside, and what you can do with it. Numbers quoted here come from real runs of this project (seed 7, 10 s, the `baseline` scenario unless stated) so you can check them yourself. Where something is simplified, the guide says so.

## Contents

1. [What NetSim Lab is](#1-what-netsim-lab-is)
2. [How an experiment works, end to end](#2-how-an-experiment-works-end-to-end)
3. [The network topology](#3-the-network-topology)
4. [Mobility approaches](#4-mobility-approaches)
5. [Scenarios](#5-scenarios)
6. [Every experiment setting](#6-every-experiment-setting)
7. [Environments: simulated, loopback, live](#7-environments-simulated-loopback-live)
8. [Combinations: what works with what](#8-combinations-what-works-with-what)
9. [The measurements and what they mean](#9-the-measurements-and-what-they-mean)
10. [The dashboard, page by page](#10-the-dashboard-page-by-page)
11. [Results, files and exports](#11-results-files-and-exports)
12. [Reproducibility](#12-reproducibility)
13. [Job management: queue, cancel, history, retention](#13-job-management-queue-cancel-history-retention)
14. [The HTTP API](#14-the-http-api)
15. [Command-line tools and campaigns](#15-command-line-tools-and-campaigns)
16. [Safety model](#16-safety-model)
17. [Honest limitations](#17-honest-limitations)
18. [What was removed and why](#18-what-was-removed-and-why)

---

## 1. What NetSim Lab is

When a phone or laptop moves from one network to another, its IP address changes and open connections break. NetSim Lab is a laboratory for one question:

> How do different mobility and handoff mechanisms affect application continuity, packet loss, interruption time and network overhead when a mobile host moves between access networks?

It models one small network, moves a mobile node between two access networks while sequence-numbered probe packets keep flowing, applies a chosen mobility mechanism, and measures what the application experiences. You can then compare mechanisms under identical conditions.

What you can do with it:

- Run one experiment and read packet loss, service gap and handoff latency.
- Change the network conditions (delays, random loss, a far-away home agent, slow movement detection) and see how each mechanism copes.
- Compare up to six finished runs side by side, with a warning if they were not run under the same conditions.
- Re-run any experiment from its stored configuration and seed and get the same numbers.
- Export raw results as JSON, CSV or ZIP.

Every result is labelled **simulated** or **loopback**. The dashboard never presents these as measurements of a real network.

## 2. How an experiment works, end to end

1. **Configure.** You pick a mobility approach, a scenario (a preset set of conditions and handoff times), a duration and, optionally, advanced settings.
2. **Validate.** The server checks every field against explicit allowed values and limits. Unknown fields are rejected, client-supplied paths and commands do not exist, and unsupported combinations are explained. The dashboard repeats the checks locally for instant feedback, but the server is the authority.
3. **Queue.** A valid request becomes a background job with an id such as `exp-0bc64bcdbd7641ae`. At most 2 jobs run at once and at most 20 wait in the queue.
4. **Run.** A separate worker process runs the experiment. The mobile node sends numbered probes to the correspondent node at a fixed rate. At the scheduled times the mobile node's active interface changes (for example from `eth-a` to `eth-b`). The chosen approach reacts: it re-registers, reconnects, or migrates.
5. **Measure.** The correspondent echoes probes back. From the sequence numbers and timestamps the system computes loss, gaps and latency per handoff. The approach also writes an event log (registrations, reconnects, retransmissions).
6. **Store.** Raw files are written to the run directory, then metrics are computed from them.
7. **Inspect.** The dashboard reads the metrics from the API and draws them. The frontend computes no measurement itself.

In the simulator, time is virtual. A 10 s run finishes in a fraction of a second of real time, and the same seed gives identical results.

## 3. The network topology

Five nodes, read from `config/topology.yaml` and shown on the topology page:

| Node | Role |
|---|---|
| CN | Correspondent node: the peer the mobile node talks to |
| HA | Home agent: keeps the mobile node reachable at its permanent home address (Mobile IP approaches) |
| ANA | Access network A: where the mobile node starts |
| ANB | Access network B: where the mobile node moves |
| MN | Mobile node: has two interfaces, `eth-a` (towards ANA) and `eth-b` (towards ANB) |

Configured links (one-way delay; other impairments in brackets):

| Link | Delay |
|---|---|
| cn–ha | 5 ms |
| ha–ana | 10 ms |
| ha–anb | 20 ms |
| cn–ana | 8 ms |
| cn–anb | 12 ms |
| ana–mn | 5 ms (50 Mbit/s) |
| anb–mn | 15 ms, 2 ms jitter (10 Mbit/s) |

Access network B is deliberately slower and more variable than A, so a handoff from A to B changes the path quality as well as the address.

## 4. Mobility approaches

Six selectable behaviours from five approaches. All share one experiment interface (same traffic, same handoff schedule, same metric definitions), so their results are comparable.

### 4.1 TCP reconnection (two recovery modes)

**Idea.** There is no mobility protocol. When the address changes, the TCP connection dies. The application notices, opens a new connection from the new address and resumes its session using its own sequence numbers.

**Recovery mode** (the "TCP loss detection" setting):

- **On network-change event.** The application learns about the link change from the operating system and reconnects quickly.
- **On timeout.** The application waits until traffic stalls past a timeout before reconnecting.

Baseline results: handoff latency 128 ms / 72 ms with notification, 608 ms / 552 ms with timeout (first and second handoff). Packet loss is 0 in both because TCP retransmits; the cost appears as delay.

**Limits.** Only applications written to resume sessions benefit. An unmodified TCP application just loses its connection. In the simulator TCP is the simplified `TcpLite` model; the same application logic is checked against real kernel TCP in loopback runs.

### 4.2 Basic Mobile IP

**Idea.** The mobile node keeps a permanent home address. After moving, it registers its new care-of address with the home agent (messages are signed and replay-protected). The home agent tunnels packets for the home address to the care-of address using IP-in-IP. Replies go direct (triangle routing).

**Handoff style.** Break-before-make: the old link disappears at the moment of movement, so packets in flight are lost.

Baseline: handoff latency 91 ms / 51 ms, service gap 110 ms / 70 ms, 8 probes lost per handoff (16 of 950 in total, 1.68 %).

**Limits.** An educational Mobile IPv4-style model. It is not RFC 5944 compliant: it uses HMAC-SHA256 instead of keyed MD5, and has no foreign agent, no ARP handling and no reverse tunnelling. See `docs/MOBILE_IP.md`.

### 4.3 Mobile IP with make-before-break

**Idea.** The same registration protocol, but the new link comes up before the old one is released. Registration is sent from the new address while the old link still carries traffic. The old link is released after a hold time (`old_link_hold_ms`) so packets already in flight can still arrive.

Baseline: handoff latency identical to basic Mobile IP (91 / 51 ms) because the signalling is the same, but the service gap drops to 30 ms / 10 ms and loss to 0. This is the clearest demonstration that handoff latency and service gap are different things.

**Limits.** It needs two usable links at once, and the benefit depends on the hold time. It is a testbed extension, not a separate protocol. It is kept distinct from basic Mobile IP because the handoff behaviour differs.

### 4.4 Route optimisation (simplified model)

**Idea.** Basic Mobile IP registration plus a return-routability exchange (home and care-of tokens) and an authenticated binding update to the correspondent. The correspondent then sends directly to the care-of address, avoiding the detour through the home agent.

Baseline: handoff latency 199 ms / 103 ms, service gap 214 ms / 116 ms, 31 of 950 probes lost (3.26 %). It is slower at the handoff because of the extra exchange, and pays off for later packets.

**Limits.** A simplified model inspired by Mobile IPv6 route optimisation (RFC 6275). It is not Mobile IPv6 and not a standards implementation.

### 4.5 QUIC connection migration

**Idea.** The application keeps one QUIC connection. When its local address changes it sends from the new address with a fresh connection ID. The server validates the new path (PATH_CHALLENGE and PATH_RESPONSE) and carries on with the same streams, with no new handshake.

Baseline: handoff latency 129 ms / 73 ms, service gap 136 ms / 76 ms, 0 probes lost (QUIC retransmits).

**How it runs.** A real QUIC engine (`aioquic`) runs inside the emulator. If `aioquic` is not installed, the option still appears in the dashboard but is disabled with an explanation. `aioquic` is listed in `requirements.txt`, so `make setup` installs it.

**Limits.** It only helps applications that use QUIC. It does not make arbitrary TCP applications mobile.

### 4.6 Summary of the five approaches

| Approach | Who handles mobility | Packet loss on handoff | Notes |
|---|---|---|---|
| TCP reconnection | The application | None (retransmitted), delay instead | Needs resume logic in the app |
| Basic Mobile IP | The network (home agent) | Yes (break-before-make) | Triangle routing, tunnel overhead |
| Mobile IP make-before-break | The network (home agent) | Reduced or none | Needs two simultaneous links |
| Route optimisation model | The network, direct to the correspondent | Yes | Extra signalling, better path later |
| QUIC migration | The transport (QUIC) | None (retransmitted) | QUIC applications only |

## 5. Scenarios

A scenario is a preset of network conditions and handoff times. Every scenario runs 10 s by default.

| Scenario | What changes |
|---|---|
| Baseline | Topology delays unchanged, no random loss. Two handoffs: `eth-a` to `eth-b` at 4 s, back at 7 s |
| Doubled link delays | Every link delay multiplied by 2 |
| 2 % loss on the access links | Independent random loss of 2 % per direction on both access links, applied to data, echoes and signalling |
| Home agent 100 ms further away | +100 ms one-way delay on every link touching the home agent |
| Movement detection takes 100 ms | The mobile node needs 100 ms instead of 20 ms to notice its link changed |
| Rapid second handoff | Handoffs at 4.0 s and 5.5 s instead of 4.0 s and 7.0 s |
| 30 % of Mobile IP registration messages lost | Extra loss only on registration requests and replies, on top of any link loss |

Selecting a scenario fills in its duration. The handoff schedule is fixed by the scenario.

## 6. Every experiment setting

**Shown by default**

| Setting | Meaning | Allowed |
|---|---|---|
| Mobility approach | Which mechanism to test | The five approaches (unavailable ones are disabled) |
| Scenario | Conditions and handoff schedule | The seven presets |
| Duration (s) | Length of the run | Greater than 0, at most 120 |
| Handoff mode | Break-before-make or make-before-break | Shown only for Mobile IP make-before-break |
| TCP loss detection | Event or timeout recovery | Shown only for TCP reconnection |

**Under Advanced settings**

| Setting | Meaning | Allowed |
|---|---|---|
| Environment | Where the run executes | `emulated`, plus `loopback` for TCP reconnection |
| Probe rate (packets/s) | How often probes are sent | Greater than 0, at most 1000, and duration × rate at most 120,000 probes |
| Payload (bytes) | Probe size | 36 to 1472 |
| Seed | Seeds the random streams (loss) | 0 to 2,147,483,647. Same seed, same result |
| Network condition overrides | Replace the scenario's own values | See below |
| Label | Free text to find the run later | At most 80 printable characters |

**Network condition overrides** (leave blank to keep the scenario's value)

| Override | Range |
|---|---|
| Latency scale | Greater than 0, up to 100 |
| Random loss (%) | 0 up to, not including, 100 |
| Home agent distance (ms) | 0 to 5000 |
| Registration loss (%) | 0 up to, not including, 100 |
| Home agent processing (ms) | 0 to 5000 |

Settings that exist in the backend but are not offered in the form (hold time, detection delay, initial interface, custom moves) keep their defaults when started from the dashboard.

## 7. Environments: simulated, loopback, live

| Environment | What it is | How measured | Available from the dashboard |
|---|---|---|---|
| `emulated` | The built-in discrete-event emulator, in virtual time | Simulator-generated | Yes |
| `loopback` | Real kernel TCP sockets on 127.0.0.1 | Real kernel sockets (loopback), not a real network | Yes, TCP reconnection only |
| `live` | Linux network namespaces with `tc netem` | Real kernel namespaces | No. Command line only, needs privileges |

The result page always names the origin ("Simulated · historical · not live"). Loopback results are marked differently from simulator results. Nothing is ever called a real-world measurement.

## 8. Combinations: what works with what

- **Approach and environment.** All five approaches run `emulated`. Only TCP reconnection runs on `loopback`. `live` is never exposed through the API.
- **Handoff mode.** Make-before-break only applies to Mobile IP make-before-break. The form hides the control for other approaches, and the server rejects the combination.
- **TCP loss detection.** Only for TCP reconnection.
- **Scenario conditions that do not apply.** Some conditions affect only some approaches. Home agent distance, home agent processing and registration loss concern Mobile IP signalling, so choosing them with TCP reconnection or QUIC produces a warning ("condition has no effect") instead of silently pretending otherwise.
- **Loss versus delay.** TCP and QUIC retransmit, so their cost appears as delay. Mobile IP and route optimisation lose probes. Compare loss only within one transport type, and compare service gap across all of them.
- **Comparing runs.** Runs should share duration, scenario, rate, payload, seed and conditions. If they differ, the compare page lists exactly which settings differ.

Examples of informative pairings (all verified results, seed 7):

| Scenario | TCP (event) loss | Basic MIP loss | MBB MIP loss | Route opt. loss | QUIC loss |
|---|---|---|---|---|---|
| Baseline | 0.00 % | 1.68 % | 0.00 % | 3.26 % | 0.00 % |
| 2 % access loss | 0.00 % | 3.37 % | 1.89 % | 4.95 % | 0.00 % |
| Distant home agent | 0.00 % | 3.16 % | 0.00 % | 3.68 % | 0.00 % |

## 9. The measurements and what they mean

| Measurement | Meaning | Unit |
|---|---|---|
| Packet loss | Probes sent but never echoed, as a percentage of probes sent, over the whole run | % |
| Service gap | Longest silence between consecutive echo arrivals around a handoff | ms |
| Handoff latency | Time from the handoff start to the approach's own recovery point (registration reply, binding update, first data on the new connection) | ms |
| Interruption | Time between the last echo before and the first after a loss burst. Defined only where a loss burst overlaps the handoff, so reliable transports report it as unavailable | ms |
| Round-trip time (RTT) | Echo time per probe: median, 95th percentile, minimum, maximum | ms |
| Goodput | Useful payload delivered per second | kbit/s |
| Mobility overhead | Extra header bytes the mobility mechanism adds per data packet (20 B for IP-in-IP tunnelling, 0 for TCP and QUIC) | B/packet |
| Signalling | Mobility or transport messages and bytes between handoff start and recovery | messages, bytes |
| Path stretch | RTT after the handoff relative to the direct path (where applicable) | ratio |

Rules the dashboard follows:

- Handoff latency is **not comparable** across approaches as a ranking, because each approach's recovery point is different. Service gap and loss are the comparable pair.
- A metric that cannot be measured is shown as **n/a**, never as zero.
- Values shown come straight from the backend's saved results. The frontend does not calculate metrics.

## 10. The dashboard, page by page

Navigation has two groups. A light/dark theme toggle sits in the header, and the choice is remembered in your browser.

### Overview (`#/overview`)
Introduction, a Configure experiment button, three counts read from the API (experiments, runnable approaches, scenarios), the topology and recent experiments.

### Network topology (`#/topology`)
A diagram of the five nodes with link delay and rate labels, and the mobile node highlighted. Below it, collapsible tables of components, links and configured impairments, and static routes with the source file.

### Configure experiment (`#/configure`)
The form from section 6, a live summary panel, a Start experiment button and a Check with server button (validates without running, and shows the configuration fingerprint). Active experiments are listed below with a Stop button. Approaches that cannot run on this server are disabled and the "About" panel says why.

### Experiment results (`#/live/<id>`)
Status, approach and scenario, then five headline values (packet loss, service gap, handoff latency, RTT, goodput) and two charts (timing per handoff, packets lost per handoff). Collapsed sections hold additional metrics, the raw per-handoff table, the event timeline, the stored configuration and metadata, and the lifecycle log. While a run is active the page polls the job status and says plainly that metrics appear only after completion. Export buttons give JSON, CSV (handoffs or events) and ZIP.

Without an id, this route lists running and recent experiments.

### Compare experiments (`#/compare`)
Select one to six completed runs. The page shows a table of metrics side by side, six charts (interruption and service gap, loss, latency, overhead, signalling volume, goodput), a collapsible table of experimental conditions, and a warning listing every setting that differs between the selected runs. It never declares an overall winner. Comparison tables can be exported.

### Experiment history (`#/history`)
All experiments with filters (status, approach), 25 per page, status badges, loss and latency summary, and links to each run. `#/history/<id>` shows one experiment in full with its configuration, lifecycle log and exports. Deleting an experiment is done through the API (`DELETE /api/experiments/{id}`), not the dashboard. The whole history can be exported as CSV or JSON.

## 11. Results, files and exports

Each run writes raw files to its job directory under `data/api/`. Important ones:

- `comparison_metrics.json`: the computed metrics. The API serves this file, so the displayed values and the stored values are the same.
- `approach_events.csv`: the event log (registrations, reconnects, retransmissions, link changes).
- Raw probe and echo records and an `experiment.json` with the full specification and software versions.

Exports:

| Export | Content |
|---|---|
| JSON | Experiment record, provenance, and the unmodified metrics |
| CSV, handoffs | One row per handoff |
| CSV, events | One row per event |
| ZIP | Raw run folder plus an export manifest with SHA-256 checksums, and the experiment record. A partial ZIP of a failed or cancelled run is available and is labelled "not results" |
| History CSV or JSON | Summary values for all listed experiments |
| Comparison export | The comparison table |

CSV cells beginning with `=`, `+`, `-` or `@` are neutralised so a label cannot inject a spreadsheet formula.

## 12. Reproducibility

- Every run stores its complete effective configuration, including the seed.
- A configuration fingerprint identifies identical settings. Starting the same configuration while it is already queued or running is refused and names the original run.
- An optional idempotency key lets a client safely retry a request without creating a duplicate.
- Same configuration and seed give the same numbers in the simulator. For example, TCP reconnection on `baseline` with seed 7 always yields handoff latencies of 128 ms and 72 ms.
- Saved results are never rewritten by later analysis.

## 13. Job management: queue, cancel, history, retention

- **Lifecycle:** queued, running, then completed, failed, cancelled or incomplete. Every transition is logged with a timestamp and note.
- **Cancel:** stops a queued or running job. A running worker process group is terminated (SIGTERM, then SIGKILL after a grace period) and partial output is removed.
- **Failure handling:** a worker that dies without a result is recorded as incomplete with the reason. A run exceeding the time limit (300 s) is killed and marked failed.
- **Restart recovery:** after a server restart, runs that were running are marked cancelled or incomplete, and a result that was already written is kept.
- **Retention:** finished experiments are deleted after 30 days by default. This is configurable, and 0 keeps them forever.
- **Limits:** 2 concurrent jobs, 20 queued, 512 MB output per worker.

## 14. The HTTP API

Base path `/api`. Interactive documentation is available at `/docs` when the API is running.

| Method and path | Purpose |
|---|---|
| `GET /api/health`, `GET /api/status` | Liveness and job-manager state |
| `GET /api/approaches`, `/scenarios`, `/topology`, `/capabilities` | Catalogue, topology, limits and accepted fields |
| `POST /api/experiments/validate` | Check a request without running it |
| `POST /api/experiments` | Start an experiment (202 with a Location header) |
| `GET /api/experiments` | History with filters, sorting and paging |
| `GET /api/experiments/{id}` | Status, configuration, progress and summary |
| `GET /api/experiments/{id}/transitions` | Lifecycle log for reliable polling |
| `POST /api/experiments/{id}/cancel` | Cancel |
| `DELETE /api/experiments/{id}` | Delete a finished experiment and its files |
| `GET /api/experiments/{id}/metrics`, `/handoffs`, `/events` | Results (409 until completed) |
| `GET /api/experiments/{id}/export` | JSON, CSV or ZIP |
| `GET /api/history/export` | History export |

Errors always use one envelope with a code, message, request id and a list of field issues. There is no WebSocket, because metrics exist only after a run finishes, so there is nothing true to stream. Clients poll. An optional bearer token (`NETSIM_API_TOKEN`) protects every route except health.

## 15. Command-line tools and campaigns

The command line carries the workflows that are not in the browser. Key `make` targets:

| Target | What it does |
|---|---|
| `make setup` | Create the virtual environment and install dependencies |
| `make check` | Read-only environment diagnostics (namespaces, `tc`, `aioquic`) |
| `make stack-start`, `make stack-stop` | Start or stop API (8000) and dashboard (5173), unprivileged |
| `make test`, `make frontend-test`, `make frontend-build` | Backend tests, frontend tests, production build |
| `make mip-sim` | Emulated Mobile IP handoff demo, no root needed |
| `make compare` | Run the standard approach comparison and write a report |
| `make topo-validate`, `topo-plan`, `topo-create`, `topo-destroy` | Validate or build the live namespace topology (needs sudo) |
| `make exp-list` | Approaches, configurations and where each can run |
| `make exp-plan` | Show the design of the example campaign without running it |
| `make exp-example` | Run or resume the 372-run example campaign, then analyse and plot |
| `make exp-standard` | Full 20-replicate campaign (1,680 runs) |
| `make exp-analyze DIR=...` | Recompute statistics and the report from saved results |
| `make exp-plots DIR=...` | Charts from the saved analysis |
| `make exp-reproduce DIR=...` | Re-run a sample of saved runs and check they are identical |

A single run from the command line:

```bash
PYTHONPATH=backend .venv/bin/python -m app.compare run \
  --approach mip-mbb --handoff-mode mbb --duration 10 --seed 7 --out-dir /tmp/run
```

**Campaigns and statistics** (command line only). A campaign repeats every approach under each condition with fixed seeds, then summarises each metric with medians, quartiles and exact distribution-free 95 % confidence intervals, compares approaches pairwise with a Wilcoxon signed-rank test with Holm correction, and adds seeded bootstrap intervals for median differences. The dashboard does not load campaign results. Method and caveats: `docs/EXPERIMENTS.md`.

## 16. Safety model

- The API never runs privileged operations and refuses to run as root (override only inside a disposable container).
- `live` namespace experiments are never reachable through the API.
- Requests accept only whitelisted fields. Paths, commands and topology files cannot be supplied by a client.
- Experiments run in separate worker processes with CPU, runtime and file-size limits.
- Privileged network commands (CLI only) use an allow-list and act only on namespaces created by this project. See `docs/PRIVILEGED_OPERATIONS.md`.

## 17. Honest limitations

- Simulator results describe the simulator's model, not a real network. Loopback runs use real sockets but no real radio or routers.
- Mobile IP is an educational model, not a standards-compliant implementation. Route optimisation is a simplified model.
- One run per configuration is one sample. For defensible differences use a campaign with replicates.
- Handoff latency is not comparable as a ranking across approaches.
- The dashboard does not show campaign results.

## 18. What was removed and why

The handoff-prediction machine-learning module and the dashboard's Research section (model evaluation, handoff prediction, campaign analysis page) were removed at the project owner's request to keep the project focused on mobility mechanisms and their measurement. This took out the `backend/app/ml` package, the model registry, the model and prediction API routes, the `predict` request fields, the prediction export table, the example model bundle and the related tests and pages. The campaign and statistics command-line tools were kept, without their prediction columns.
