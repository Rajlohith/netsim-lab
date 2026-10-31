# NetSim Lab — Demonstration and Presentation Guide

How to set the project up for a demo, what to click in what order, what to say, and how to answer the questions you are likely to get. Every number quoted below comes from a real run of this project and is **repeatable**: the emulator is deterministic, so the same approach, scenario, duration and seed give the same values on your machine.

## Contents

1. [The project in one breath](#1-the-project-in-one-breath)
2. [Before the demo](#2-before-the-demo)
3. [The 10-minute dashboard demo](#3-the-10-minute-dashboard-demo)
4. [The numbers you can quote](#4-the-numbers-you-can-quote)
5. [Explaining the networking concepts simply](#5-explaining-the-networking-concepts-simply)
6. [What makes the project good (say these out loud)](#6-what-makes-the-project-good-say-these-out-loud)
7. [Optional extra demos](#7-optional-extra-demos)
8. [Questions you will be asked, with honest answers](#8-questions-you-will-be-asked-with-honest-answers)
9. [What not to claim](#9-what-not-to-claim)
10. [If something goes wrong](#10-if-something-goes-wrong)
11. [Cheat sheet](#11-cheat-sheet)

---

## 1. The project in one breath

**30 seconds.**
"When your phone or laptop moves from one network to another, its IP address changes and connections break. There are several ways to repair that. I built a lab that implements five of them in the same test bench, runs each one under identical conditions, measures how many packets are lost and how long the service stalls, and shows the results in a web dashboard. It also has a statistics pipeline for repeated experiments and a small machine-learning module that tries to predict handoff trouble."

**2 minutes.** Add:
* "The five approaches are: simply reconnecting TCP, Mobile IP, Mobile IP with make-before-break, a route-optimisation model, and QUIC connection migration."
* "A stream of numbered UDP probes keeps flowing while the mobile node moves between two access networks, so loss is counted exactly from sequence numbers."
* "Everything is saved as raw files, so every chart and number can be traced and every experiment can be reproduced from its seed."
* "There is a FastAPI backend with job queueing, cancellation and exports, and a React dashboard with seven pages."
* "All the networks are simulated or loopback. I say that on every screen because the results describe the emulator, not the real Internet."

## 2. Before the demo

Do this **the evening before**, then again 30 minutes before.

### Checklist

- [ ] Laptop on charger, notifications off (KDE: *Do Not Disturb*), browser zoom 100 %, window maximised
- [ ] `cd ~/netsim-lab` and `make check` is clean
- [ ] `make test-report` ran once successfully (so you can say the tests pass on **this** machine)
- [ ] `make frontend-build` succeeded
- [ ] `make stack-start`, then open http://127.0.0.1:5173. The header shows **API: reachable**
- [ ] Pre-run the experiments below so the comparison page is full (they finish in seconds each)
- [ ] Browser tabs ready: dashboard, `http://127.0.0.1:8000/docs`, and the project README
- [ ] A terminal ready in the project root, font enlarged
- [ ] A second plan: screenshots from `docs/screenshots/` open in a folder, in case the live demo fails

### Pre-run these four experiments

In **Experiment configuration**, use duration **12 s**, seed **1**, label **demo**:

| # | Approach | Scenario |
|---|---|---|
| 1 | Mobile IP (basic) | baseline |
| 2 | Mobile IP with make-before-break | baseline |
| 3 | TCP reconnection | baseline |
| 4 | Route optimisation (model) | 2 % loss on the access links (`lossy-access`) |

The dashboard now has real data for the live page, the comparison page and the history page.

### Clean slate between rehearsals

Old runs are harmless. If you want a clean history: stop the stack (`make stack-stop`), delete `data/api/`, start again.

## 3. The 10-minute dashboard demo

Times are a guide. Each step has **do**, **say**, and **point at**.

### 0:00 — Topology (1 min)
**Do:** open **Network topology**.
**Say:** "This is the lab. A correspondent node on the left, the home agent in the middle, two access networks, and the mobile node, which can be attached to either. The delays and bandwidths you see are read live from the backend, not drawn by hand. On a real machine each box is a Linux network namespace and the links are shaped with `tc netem`; for this demo I'm using the emulator, which needs no root."
**Point at:** the two access links: A is fast and close (5 ms, 50 Mbit/s), B is slower (15 ms, 10 Mbit/s). "So moving from A to B changes the path."

### 1:00 — Configure and start an experiment (2 min)
**Do:** open **Experiment configuration**. Choose **Mobile IP (basic)**, scenario **baseline**, duration 12, seed 1, click **Start experiment**.
**Say:** "Every approach is selected from the same form because they all run through one common interface. The form validates before anything is queued. Try an invalid duration" (type −5, show the error) "and nothing is created."
**Point at:** the approach description: "It tells you what each approach is and what it is *not*. This Mobile IP is educational, not a full RFC implementation."
**Do:** the page moves to the live view while the run is queued → running → completed.
**Say:** "Behind this button is a real worker process. The backend queues it, runs it in its own process group, and I can cancel it at any time."

### 3:00 — Live metrics (2 min)
**Do:** stay on the live page of a finished run (use the pre-run Mobile IP experiment if the new one is still running).
**Point at, in order:**
1. The origin badge: **Simulated · historical · not live**. "I'm not allowed to pretend this is live, so the page says so."
2. The headline values: RTT median 33 ms, packet loss 16 of 1,150 probes, mobility overhead 20 bytes per packet.
3. The handoff table: two handoffs, "the mobile node moved A→B at 4 s and back at 7 s."
4. The timing chart: handoff latency, service gap, interruption per handoff.
5. The event timeline: "Each dot is a protocol event: link change, attach, registration signal sent and received, recovered."
**Say:** "The 20 bytes per packet is the IP-in-IP tunnel header the home agent adds. That is the price of Mobile IP, and it is measured, not assumed."

### 5:00 — Approach comparison (2 min)
**Do:** open **Approach comparison**, tick the four pre-run experiments.
**Say:** "Now the same stream under different mechanisms." Then point at the **yellow warning**: "The page noticed that one experiment used a different scenario and warns that these runs were not made under identical conditions. I would rather it nag than let me draw a wrong conclusion."
**Point at:** the service-gap chart. "Make-before-break has the smallest gap because the new link is up before the old one is released. TCP reconnection stalls for about a hundred milliseconds while it notices and reconnects. The route-optimisation run is higher, but remember it also had 2 % loss, so this is not a fair comparison, and the page tells you so."
**Say:** "This page is for looking at single runs. For statements about which approach is better, I use replicated campaigns with confidence intervals, which I'll show next."

### 7:00 — Model evaluation and prediction (1 min)
**Do:** open **Model evaluation**.
**Say:** "I also trained three predictors to flag an upcoming handoff problem: a simple threshold rule, logistic regression and a random forest, evaluated on whole scenario families that were never seen in training. The honest result is on this page: the threshold rule does **not** beat simply always raising the alarm, and the models only do modestly better. The reason is that my emulator has no gradual degradation before a handoff, so there is nothing real to predict from. I put that caveat in the dashboard rather than hide it."

### 8:00 — History and export (1 min)
**Do:** open **Experiment history**. Open a run, click **JSON**, **CSV · handoffs**, **ZIP · raw run files**.
**Say:** "Every run is kept, filterable, and exportable. The ZIP contains the raw probe files, so anyone can recompute the metrics. The numbers in these exports are byte-identical to what the API returns, which I check automatically."

### 9:00 — Wrap-up (1 min)
**Say:** "To summarise: five approaches, one interface, exact measurements, honest labelling, reproducible experiments, a safe backend, and a dashboard that only shows real data. The limits are that it's simulated and Mobile IP is educational. Questions?"

## 4. The numbers you can quote

All from the shipped project, seed 1, 12-second runs at 100 probes per second, baseline unless stated. Re-run to confirm on your machine.

| Run | Handoffs | Probes lost | Service gap (mean) | Overhead |
|---|---|---|---|---|
| Mobile IP (basic) | 2, both recovered | 16 of 1,150 (1.39 %) | 90 ms (110 ms and 70 ms) | 20 B/packet |
| Mobile IP make-before-break | 2 | 0 (0 %) | 20 ms | 20 B/packet |
| TCP reconnection (notify) | 2 | 0 (reliable transport; loss shows up as delay) | 105 ms | none |
| Route optimisation, 2 % access loss | 2 | 4.78 % | 170 ms | 20 B/packet |

For **statements with statistics**, use the 372-run example campaign (12 replicates per configuration, baseline medians of application stall):

| Approach | Median stall | 
|---|---|
| Mobile IP, make-before-break | 20 ms |
| Basic Mobile IP | 80 ms |
| TCP reconnection with OS notification | 104.75 ms |
| Route optimisation (model) | 160 ms |
| TCP reconnection with stall timeout | 636.25 ms |

Suggested sentence: "In my emulator, under the baseline conditions, make-before-break Mobile IP interrupts the application for the shortest time and TCP with a stall timeout for the longest, about thirty times longer. The full report gives confidence intervals; in this deterministic emulator several of them are zero-width."

Other facts worth knowing:
* 372 experiments in the example campaign, all completed; regenerated and compared against the earlier result with **zero differences**; 10 of 10 sampled runs reproduced byte for byte.
* 552 backend tests ran: 526 passed, 0 failed, 26 skipped (environmental).
* 31 browser checks passed in real headless Chrome.
* 5 approaches, 7 scenarios, 7 dashboard pages, 21 API endpoints.

## 5. Explaining the networking concepts simply

**Handoff.** The moment a mobile device switches from one network attachment to another.

**Why connections break.** A TCP connection is identified by the two IP addresses and ports. If your address changes, the other side no longer recognises you.

**Mobile IP, in one analogy.** "It's postal redirection. The mobile node always keeps a permanent *home address*. A *home agent* at home receives mail for it and forwards it, in an envelope inside an envelope (IP-in-IP tunnelling), to wherever the mobile node currently is, its *care-of address*. When the node moves it sends a signed *registration* to the home agent: 'forward to this new address instead.'" The 20 bytes per packet is the extra outer envelope.

**Triangle routing.** Packets to the mobile node go via the home agent, but its replies go straight back, so the path forms a triangle. It is longer than necessary.

**Route optimisation.** The mobile node tells the correspondent its current address directly, after proving it owns it (the *Return Routability* check), so traffic can skip the home agent. "I implemented a simplified model of this idea; it belongs to Mobile IPv6, not IPv4, and I say so."

**Make-before-break vs break-before-make.** Break-before-make drops the old link and then attaches to the new one, so packets in between are lost. Make-before-break brings the new link up first so the two overlap.

**TCP reconnection.** No mobility protocol at all. The application notices its connection died, opens a new one from the new address and resumes. It is simple but needs application support and is slow if detection relies on a timeout.

**QUIC connection migration.** QUIC connections are identified by a connection ID rather than by addresses, so the connection can move to the new address after validating the new path. It only helps applications that use QUIC.

**Service gap / interruption.** The period during which the application received nothing. This is the number users feel.

**Why sequence numbers.** Each probe carries a number, so the receiver knows *exactly* which ones are missing. Loss is counted, never guessed.

**Namespaces and `tc netem`.** Namespaces give separate network stacks inside one Linux machine; `netem` adds delay, loss and bandwidth limits to a link. Together they let one laptop pretend to be a network.

## 6. What makes the project good (say these out loud)

Evaluators and viewers rarely see this unless you point at it.

1. **Honesty is built in.** Every value carries a provenance label; unmeasurable values show `—`, never zero; simplified models are labelled as models. The dashboard repeats "simulated, not live."
2. **Reproducibility.** Fixed seeds, raw files kept, and a command that re-runs saved experiments and checks they are byte-identical.
3. **One interface for five approaches.** The comparison is meaningful because the traffic, topology and schedule are identical.
4. **Real statistics.** Medians with exact confidence intervals, paired Wilcoxon tests with Holm correction, and a rule that an approach is only "better" if the evidence supports it.
5. **Safety.** Never runs as root; only allow-listed `ip` commands are elevated; project namespaces are prefixed so nothing of yours is ever touched; the API cannot start privileged operations.
6. **Engineering depth.** Job queue, worker processes in their own process groups, cancellation, crash recovery, idempotent submission, duplicate protection, exports.
7. **Tested in layers.** Unit, service, process-level, API, frontend view tests and a real-browser check that compares on-screen numbers with the API.
8. **A negative result reported honestly.** The prediction experiment shows that without realistic signal degradation there is little to predict, and the project says so.

## 7. Optional extra demos

### 7.1 The command line (2 minutes, impressive and quick)

```bash
cd ~/netsim-lab
PYTHONPATH=backend .venv/bin/python -m app.compare list
PYTHONPATH=backend .venv/bin/python -m app.compare run \
  --approach mip-basic --seed 1 --duration 12 --out-dir data/demo
ls data/demo/mip-basic-emulated/run-1
```

**Say:** "These are the raw files: what was sent, what came back, what the protocol did. Everything the dashboard shows is computed from these."

### 7.2 Reproducibility check (1 minute)

```bash
PYTHONPATH=backend .venv/bin/python -m app.compare reproduce examples/experiments/stage7 --sample 3
```

Expected: `checked 3, identical 3, mismatches []`. **Say:** "It re-ran three saved experiments from their recorded configuration and the output files are byte-for-byte identical."

### 7.3 The statistical report

Open `examples/experiments/stage7/analysis/REPORT.md` and one chart from `analysis/plots/`. **Say:** "372 runs. One factor changed at a time around the baseline. Notice that failed or not-executed cells are listed, not hidden."

### 7.4 The API directly

Open http://127.0.0.1:8000/docs, expand `POST /api/experiments`, **Try it out**, execute, then `GET /api/experiments/{id}/metrics`. **Say:** "The dashboard is just one client. Anything can drive this API."

### 7.5 The real network lab (only if you have tested it on your machine beforehand)

```bash
make topo-plan        # shows the exact commands, no root needed
```

Then, only if rehearsed: `make topo-create`, `make topo-status`, `make topo-destroy`. **Say:** "Every command is checked against an allow-list first, and cleanup only removes namespaces that start with `nsl-`." Do **not** attempt this live if you have not run it successfully on this exact machine.

### 7.6 Safety demonstration (30 seconds)

Run `sudo .venv/bin/python scripts/api_smoke.py`. It stops with *"refusing to start the job manager as root"*. **Say:** "The application refuses to run privileged by design." (Run it once beforehand to be sure of the output on your machine.)

## 8. Questions you will be asked, with honest answers

**Is this real network data?**
No. The default environment is a deterministic emulator in virtual time, plus a loopback environment on one machine. It reflects the models and parameters I chose, which I state on every screen. The real namespace lab exists for privileged runs, but the reported results come from the emulator.

**Why not use a real network or real phones?**
Control and repeatability. With an emulator I can change one factor at a time, repeat 12 times with fixed seeds, and reproduce the result byte for byte. A real network adds noise that would need far more runs and equipment.

**Is your Mobile IP a real implementation of the RFC?**
No, and I say so. It implements the core ideas: registration with lifetime, binding at the home agent, IP-in-IP tunnelling, replay protection. It uses HMAC-SHA256 instead of keyed MD5, has no foreign agent and no reverse tunnelling, so I do not claim RFC 5944 compliance.

**Is route optimisation implemented?**
As a simplified model, labelled as such. Route optimisation is a Mobile IPv6 feature; I model its main idea (return-routability check, binding update, direct path) on top of my IPv4-style Mobile IP.

**Why is TCP reconnection stall-timeout so much worse?**
Because the application only notices the problem after a timeout, whereas with an OS notification it reacts immediately. The difference between those two variants is the cost of slow failure detection.

**Does the QUIC approach work?**
It uses the real `aioquic` library, but only when that library is installed. If it isn't, the dashboard reports it as unsupported on this server rather than faking a result.

**Why is make-before-break so good? Isn't that unrealistic?**
It assumes the device can hold two links at once and that the old link can be released after registration. Its benefit depends on that hold time. It is a real technique but needs hardware support, so the scope limit is stated on the approach page.

**Your ML model barely beats "always alarm". Why show it?**
Because that is the truth, and it is a useful result. The emulator has no gradual signal degradation before a handoff, so there is little to learn. Any skill comes from patterns in how I generated the scenarios. I split by scenario family so I'm not fooling myself with leakage, and I list the features I could not use (signal strength, position) instead of inventing them.

**How do you know your numbers are right?**
Loss comes from sequence numbers, so it is exact. Timings come from monotonic clocks. Tests check metrics against controlled scenarios where the answer is known, and a browser test compares what the dashboard shows with what the API returns.

**What if an experiment crashes?**
The run is marked `incomplete` or `failed`, the worker's process group is killed, partial outputs from cancelled runs are deleted, and it is shown in the history. After a server restart, running experiments are marked incomplete and orphaned workers are killed.

**Is it secure?**
Within its scope: validated inputs, no shell, no arbitrary commands, a minimal worker environment, an optional bearer token, and privileged operations restricted to an allow-list of `ip` commands on project-prefixed namespaces. With no token set, anyone on your machine can use the API, so don't expose it.

**Why polling instead of live updates?**
Runs execute in virtual time and produce metrics when they finish, so there is nothing to stream. The page says so. Adding live streaming would require a different execution model.

**What would you do next?**
Add a real wireless-like degradation model so prediction becomes meaningful, run the privileged lab on more kernels, add jitter to the comparison metrics, add token entry to the dashboard, and test more browsers and screen sizes.

**What was the hardest part?**
Be ready with your own answer. Good candidates: keeping measurement code separate from protocol code so metrics are trustworthy; making cancellation and cleanup safe when worker processes die; or designing the comparison so unlike runs cannot be silently compared.

**What did you learn?**
Pick two: how mobility mechanisms trade overhead for recovery time; why experiments need seeds and raw data; why an honest negative result is better than a flattering one.

## 9. What not to claim

* Do **not** say the results show how real networks or phones behave.
* Do **not** say "Mobile IP is fully implemented" or "RFC compliant".
* Do **not** say route optimisation is "Mobile IPv6".
* Do **not** say QUIC makes any TCP application mobile.
* Do **not** say the model "predicts handoffs" in general; say it evaluates a prediction pipeline on simulated data.
* Do **not** say one approach is "the best" from a single run; the comparison page is observation, the campaign report is evidence.
* Do **not** say the dashboard is "live"; it polls and shows results after completion.
* Do **not** say the privileged lab was validated on every kernel.
* Do **not** quote a number you have not re-run on the demo machine.

## 10. If something goes wrong

| Problem | What to do |
|---|---|
| Header says *API: unreachable* | `make stack-status`; `make stack-stop && make stack-start`; check `.run/logs/api.log` |
| Port already in use | `make stack-stop`; if another program owns the port, close that program (the dashboard expects the API on port 8000) |
| An experiment is stuck in *queued* | `make stack-status` shows the job manager; wait, or cancel from the configuration page |
| A page shows an error banner | Say what it means (the dashboard shows the API's real error message), go to the next page, return later |
| Nothing works at all | Switch to `docs/screenshots/` and the CLI demo (7.1), which needs no web server |
| You forgot a number | Open the page and read it; never guess |
| An unexpected question | "That's outside what I tested; here is what I did verify…" is always acceptable |

Keep a calm sentence ready: "That's a known limitation, and it's documented in the README under Known Limitations."

## 11. Cheat sheet

```bash
# start / stop
make stack-start        make stack-status        make stack-stop

# URLs
http://127.0.0.1:5173            dashboard
http://127.0.0.1:8000/docs       API docs

# quick CLI demo
PYTHONPATH=backend .venv/bin/python -m app.compare run --approach mip-basic --seed 1 --duration 12 --out-dir data/demo

# reproducibility
PYTHONPATH=backend .venv/bin/python -m app.compare reproduce examples/experiments/stage7 --sample 3
```

| Page | Route | One-line message |
|---|---|---|
| Topology | `#/topology` | The lab, read from the API |
| Configure | `#/configure` | One form, five approaches, validated |
| Live metrics | `#/live/<id>` | Exact measurements, labelled as simulated |
| Prediction | `#/predictions/<id>` | Alarms and scores per decision |
| Compare | `#/compare` | Side by side, with a warning when unfair |
| Models | `#/models` | Held-out evaluation and honest caveats |
| History | `#/history` | Everything kept, everything exportable |

**Three sentences to end on:**
"The point of the project is not that one approach wins; it's that the comparison is fair, measured, reproducible and honestly labelled. The limits are that it is simulated and that Mobile IP here is educational. Everything shown can be re-run from the repository with the commands in the README."
