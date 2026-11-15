# NetSim Lab: how to demo it to your teacher

A step-by-step plan for a 15 to 20 minute demonstration that shows every feature of the project, with what to click, what to say and what numbers to expect. Read `docs/FEATURES.md` first for the full explanation of each feature. Practise the whole demo once before presenting.

## Contents

1. [The story you are telling](#1-the-story-you-are-telling)
2. [Before the demo (10 minutes)](#2-before-the-demo-10-minutes)
3. [The numbers you should see](#3-the-numbers-you-should-see)
4. [The demo script](#4-the-demo-script)
5. [Optional command-line demos](#5-optional-command-line-demos)
6. [Explaining the concepts simply](#6-explaining-the-concepts-simply)
7. [Questions you may be asked](#7-questions-you-may-be-asked)
8. [What not to claim](#8-what-not-to-claim)
9. [If something goes wrong](#9-if-something-goes-wrong)
10. [One-page cheat sheet](#10-one-page-cheat-sheet)

---

## 1. The story you are telling

Keep one thread through the whole demo:

> "When a device moves between networks, its IP address changes and connections break. There are different ways to repair that: the application can reconnect, the network can forward traffic with Mobile IP, or the transport can migrate the connection with QUIC. NetSim Lab lets me run these mechanisms on the same network, move a device between two access networks, and measure what the application actually experiences."

Three ideas to land:

1. **Fair comparison.** Every approach runs on the same topology with the same traffic and handoff schedule.
2. **Honest measurement.** Results come from the backend's saved files. Unavailable values are shown as n/a, and simulated results are labelled as simulated.
3. **Reproducibility.** The same configuration and seed give the same numbers.

## 2. Before the demo (10 minutes)

### Setup checklist

```bash
cd ~/Desktop/GitHub/netsim-lab
source .venv/bin/activate
pip install -r requirements.txt     # once, installs aioquic so QUIC is selectable
chmod +x scripts/*.sh               # only if you extracted the project from a zip
make check                          # environment diagnostics
make test                           # optional: backend tests (about 1 to 2 minutes)
make stack-start                    # API on :8000 and dashboard on :5173
```

Open `http://127.0.0.1:5173` in a browser. Check that the header shows **API reachable**.

Check these before presenting:

- [ ] The **Configure experiment** page lists **QUIC migration** as selectable (not greyed out). If it is greyed out, `aioquic` is missing: run `pip install -r requirements.txt`, then `make stack-stop && make stack-start`.
- [ ] Browser zoom is 110 to 125 % so the back row can read it. Close other tabs and notifications.
- [ ] Decide light or dark theme (the toggle is in the header, top right). Dark often projects better in a bright room and light better in a dark room.
- [ ] Optional but recommended: run the five baseline experiments once before the audience arrives (section 4, step 4), so the history is not empty if something fails. Old runs in the history do not hurt the demo.
- [ ] Have `docs/FEATURES.md` open in a second window for questions.

### Terminal tabs to keep open

1. The project folder with the virtual environment active (for `make` commands).
2. A second tab for `make stack-stop` at the end.

## 3. The numbers you should see

Under the **Baseline** scenario, with default settings, any seed (there is no random loss in this scenario, so results are identical every time). Two handoffs happen: `eth-a` to `eth-b` at 4 s and back at 7 s. The dashboard shows the mean of the two handoffs with the maximum underneath.

| Approach | Packet loss | Service gap (mean, max) | Handoff latency (mean, max) | Per handoff latency |
|---|---|---|---|---|
| TCP reconnection, on network-change event | 0 % (0 of 950) | 105 ms, 135 ms | 100 ms, 128 ms | 128 / 72 ms |
| TCP reconnection, on timeout | 0 % (0 of 950) | 585 ms, 615 ms | 580 ms, 608 ms | 608 / 552 ms |
| Basic Mobile IP | 1.68 % (16 of 950) | 90 ms, 110 ms | 71 ms, 91 ms | 91 / 51 ms |
| Mobile IP make-before-break | 0 % (0 of 950) | 20 ms, 30 ms | 71 ms, 91 ms | 91 / 51 ms |
| Route optimisation (model) | 3.26 % (31 of 950) | 165 ms, 214 ms | 151 ms, 199 ms | 199 / 103 ms |
| QUIC migration | 0 % (0 of 950) | 106 ms, 136 ms | 101 ms, 129 ms | 129 / 73 ms |

Under **2 % loss on the access links** (random loss, so use seed 7 to match exactly), the loss column becomes: TCP 0 %, basic Mobile IP 3.37 %, make-before-break 1.89 %, route optimisation 4.95 %, QUIC 0 %. Under **Home agent 100 ms further away** (seed 7): basic Mobile IP 3.16 %, make-before-break 0 %, route optimisation 3.68 %, TCP and QUIC 0 %.

Your numbers may differ if you change duration, rate or payload. If a number differs from this table, trust the dashboard, which reads the backend's saved results, and explain the difference by the settings you changed.

## 4. The demo script

Times are suggestions. Total about 18 minutes.

### Step 1. Introduce the project (1 minute) · Overview page

Click **Overview**.

Say: "This is NetSim Lab, a laboratory for studying mobility and handoff in IP networks. These counts are read live from the backend: how many experiments have been run, how many mobility approaches are runnable here, and how many scenarios exist. Everything is labelled simulated or loopback, never a live network."

Point at the footer of the sidebar: "Simulated and loopback data only."

### Step 2. The topology (2 minutes) · Network topology

Click **Network topology**.

Say, pointing at the diagram:

- "CN is the correspondent node, the peer the mobile device talks to."
- "MN is the mobile node. It has two interfaces, one to access network A and one to access network B."
- "HA is the home agent. In Mobile IP it keeps the device reachable at its home address."
- "The labels show each link's delay and rate. Network B is slower and has jitter, so moving there also changes path quality."
- "The handoff is the mobile node leaving A and attaching to B while traffic is flowing."

Open **Links and configured impairments**: "These values come from the topology file through the API, not from the frontend."

### Step 3. Configure one experiment (3 minutes) · Configure experiment

Click **Configure experiment**.

1. Leave **Mobility approach** on TCP reconnection and open **About TCP reconnection baseline**: read the mechanism and limits aloud. "Every approach states how it works, its standards status and its limits."
2. Change the approach to **Mobile IP (basic)**. Point out that **Scenario** and **Duration** are visible, and the **Summary** panel on the right updates.
3. Open the **Scenario** list and read two: "Baseline" and "2 % loss on the access links". "A scenario is a preset of network conditions and handoff times."
4. Expand **Advanced settings**. Show probe rate, payload, seed, environment and the network condition overrides. Say: "These are hidden by default. Researchers can override any condition, but a first run does not need them."
5. Click **Check with server**. Show the message "The server accepts this configuration (fingerprint …)". Say: "The server validates everything. The browser checks are only for convenience."
6. Click **Start experiment**.

### Step 4. Read a result (3 minutes) · Experiment results

The page opens automatically.

Say:

- "Status, approach and scenario are at the top, with a badge saying Simulated, historical, not live. Metrics only exist after a run ends, so I do not call this live."
- "Packet loss is the percentage of probes never echoed. Basic Mobile IP loses 16 of 950 probes, 1.68 %, because it is break-before-make."
- "Service gap is the longest silence the application saw around a handoff. Handoff latency is how long the mechanism took to reach its own recovery point."
- Point at the two charts: "Timing per handoff and packets lost per handoff."

Open the collapsed sections one by one, quickly:

- **Additional metrics**: "RTT percentiles, overhead per packet (20 bytes for the IP-in-IP tunnel), handoff count."
- **Raw handoff measurements**: "One row per handoff, straight from the saved file."
- **Event timeline**: "The approach's own event log. Registration requests and replies, link changes."
- **Experiment configuration and metadata**: "The exact settings and the seed, so I can reproduce this run."

Click **Export results** and download one **CSV · handoffs** and mention JSON and ZIP: "The ZIP has the raw files and a checksum manifest."

### Step 5. Run the other approaches (2 minutes) · Configure experiment

Go back to **Configure experiment** and start these, one after another, changing only the approach (and the recovery mode or handoff mode where it appears):

1. TCP reconnection with **On network-change event**
2. TCP reconnection with **On timeout**
3. Mobile IP make-before-break (set **Handoff mode** to make-before-break)
4. Route optimisation (model)
5. QUIC migration

Say while they run: "At most two run at once and the rest wait in the queue. Each is a separate process with limits, so one bad run cannot take down the server." The **Active experiments** list at the bottom of the page shows queued and running jobs.

If you start the same configuration twice while the first is still running, the server refuses and names the original. You can mention this: "Duplicate active runs are blocked."

### Step 6. Compare approaches (4 minutes) · Compare experiments

Click **Compare experiments**. Tick the six runs you just made (the limit is six).

Say and show:

1. **Comparison table**: "Same conditions, same seed. Look at loss: TCP and QUIC lose nothing because they retransmit. Their cost is delay. Mobile IP loses packets."
2. Point to **service gap** in the table and the chart **Interruption time and service gap**: "Basic Mobile IP and make-before-break have exactly the same handoff latency because the signalling is identical. But make-before-break's service gap drops from 90 ms to 20 ms, and loss goes to zero. That is the whole point of keeping the old link up briefly."
3. "TCP on timeout is the slowest, about 580 ms, because the application waits for a stall before reconnecting. With a notification it is about 100 ms. QUIC is about 101 ms with no new handshake."
4. "Route optimisation is slower at the handoff, about 151 ms, because it adds a return-routability exchange. Its benefit is a more direct path afterwards, so I would not call it better or worse overall."
5. Say clearly: "The dashboard does not pick a winner. Handoff latency is measured to each approach's own recovery point, so it is not a fair ranking. Service gap and packet loss are the comparable pair."
6. Open **Experimental conditions**. To show the warning feature: tick in one run that used a different duration (start one more run with duration 12 beforehand) and point to the yellow notice listing exactly which settings differ. "If runs are not comparable, the page says why."

Click an export button on this page and mention that the comparison table can be exported.

### Step 7. Show that conditions matter (2 minutes) · Configure experiment

Start two runs with seed 7 and **Scenario: 2 % loss on the access links**: basic Mobile IP and make-before-break. Open **Compare experiments** and select them.

Say: "Now random loss hits data, echoes and signalling. Basic Mobile IP loses 3.37 %, make-before-break 1.89 %. Make-before-break still wins on loss, but both are worse, because a lost registration message delays recovery."

Optional: choose **Scenario: Home agent 100 ms further away** with TCP reconnection. The form shows a warning that the condition has no effect on TCP. Say: "The system tells you when a condition cannot affect an approach, instead of pretending."

### Step 8. Validation and honesty features (1 minute) · Configure experiment

- Type `500` in **Duration**: the form shows `> 0, ≤ 120`. "Limits are enforced twice, in the browser and on the server."
- If you want to show the unavailable-option behaviour, say: "If `aioquic` is not installed, QUIC is greyed out with the reason, instead of offering something that cannot run."

### Step 9. History and reproducibility (2 minutes) · Experiment history

Click **Experiment history**.

- Use the **Status** and **Approach** filters.
- Click an experiment id to open its details: configuration, lifecycle log and exports.
- Say: "Every experiment keeps its full configuration and seed. If I start the same configuration again I get exactly the same numbers. For example, TCP reconnection with notification always gives 128 and 72 milliseconds."
- Demonstrate: finish one run, start the identical configuration again, and open both in **Compare experiments**. The values match.
- Mention: "The whole history can be exported as CSV or JSON."

### Step 10. Theme and responsiveness (30 seconds)

Click the sun or moon button in the header to switch theme. "The preference is remembered. Charts, tables and the topology diagram follow the same theme." Narrow the browser window to show the sidebar turning into a menu.

### Step 11. Close (30 seconds)

Say: "To summarise: five mobility approaches on one topology, seven scenarios, a validated background job system, raw result files with exports, comparison with fairness warnings, and reproducible runs. The simulator is deterministic, and results are honestly labelled as simulated."

## 5. Optional command-line demos

Use these if your teacher wants more depth, or if you have spare time.

| Demo | Command | What to say |
|---|---|---|
| Emulated Mobile IP handoff in the terminal | `make mip-sim` | "The same mechanism without the web layer." |
| Approaches and where each can run | `make exp-list` | "Machine-readable list of every approach and environment." |
| Standard comparison with a report | `make compare` | "Runs all approaches and writes a comparison report." |
| One run from the command line | `PYTHONPATH=backend .venv/bin/python -m app.compare run --approach mip-mbb --handoff-mode mbb --duration 10 --seed 7 --out-dir /tmp/run` | "Same engine as the dashboard." |
| Design of a statistical campaign | `make exp-plan` | "Campaigns repeat runs with fixed seeds for confidence intervals and statistical tests." |
| Reproducibility check | `make exp-reproduce DIR=examples/experiments/stage7` (needs raw runs, see note) | "Re-runs saved runs and checks they are identical." |
| API documentation | open `http://127.0.0.1:8000/docs` | "Every endpoint is documented and typed." |
| Test suites | `make test` and `make frontend-test` | "Backend and frontend tests." |

Note: `examples/experiments/stage7` ships the saved analysis only. To reproduce runs you must first generate raw data with `make exp-example` (about 6 to 8 minutes), so do that before the demo if you want to show it. `make exp-analyze` and `make exp-plots` also need the raw data.

Campaign statistics, if asked: "Each cell is repeated with fixed seeds. I report medians and quartiles with exact distribution-free 95 % confidence intervals, compare approaches with a Wilcoxon signed-rank test with Holm correction for multiple comparisons, and use seeded bootstrap intervals for the median difference. This is command-line only."

## 6. Explaining the concepts simply

- **Handoff.** A device moving from one access network to another.
- **Service gap.** How long the application saw no data around the handoff. This is what the user feels.
- **Handoff latency.** How long the mobility mechanism took to finish its own repair work.
- **Break-before-make.** The old link disappears before the new one works, so packets in flight are lost.
- **Make-before-break.** The new link is up before the old one is released, so packets in flight still arrive.
- **Mobile IP.** The device keeps one permanent home address. A home agent forwards traffic to wherever the device currently is, through a tunnel.
- **Triangle routing.** Packets to the device go via the home agent, but replies go directly, so the path is a triangle.
- **Route optimisation.** The correspondent learns the device's current address and sends directly, skipping the home agent.
- **TCP reconnection.** No mobility protocol. The application notices the broken connection and reconnects.
- **QUIC migration.** The same QUIC connection continues from a new address after the new path is validated.
- **Loopback versus simulator.** The simulator runs in virtual time. Loopback uses real operating-system TCP sockets on one machine. Neither is a real wireless network.

## 7. Questions you may be asked

**Why does TCP show 0 % loss but a long stall?** TCP retransmits, so lost data is re-sent. The cost appears as delay, not lost packets. That is why loss should be compared only within the same transport and service gap across all.

**Why can't you say which approach is best?** Each has trade-offs. QUIC needs QUIC applications, Mobile IP needs infrastructure (a home agent), make-before-break needs two links, and route optimisation adds signalling. Handoff latency is measured to different recovery points, so it is not a ranking.

**Is this real network data?** No. It is simulator-generated data (and real loopback sockets for TCP). The dashboard labels this on every result. A real wireless network would add radio effects the simulator does not model.

**Is your Mobile IP standards compliant?** No. It is an educational model inspired by Mobile IPv4. It uses HMAC-SHA256 instead of keyed MD5 and omits foreign agents, ARP and reverse tunnelling. The route-optimisation approach is a simplified model of Mobile IPv6 route optimisation, not Mobile IPv6.

**How do you know the numbers are right?** The dashboard shows values read from the backend's saved result files. The backend has an extensive test suite that checks metrics against the raw data, and the same seed reproduces identical results.

**Why is QUIC's handoff latency similar to TCP's?** In this model both need the new path to carry data before traffic resumes. QUIC avoids a new handshake and keeps its streams, but still waits for the link change to be detected and the new path validated.

**What does the seed do?** It seeds the random streams used for packet loss, and it is stored with every run so the run can be reproduced.

**Where does randomness come from in the baseline?** There is none. The baseline has no random loss, which is why its results are identical for any seed.

**How did you make it safe?** The API runs unprivileged, refuses to run as root, never exposes the privileged live-namespace mode, accepts only whitelisted fields, and runs each experiment in a separate limited process. Privileged network commands (command line only) use an allow-list and only touch namespaces created by this project.

**What about statistics?** One run is one sample. For defensible comparisons the command-line campaign tools repeat runs with fixed seeds and report confidence intervals and tests. The dashboard shows single runs and says so.

**Did you remove anything?** Yes. A machine-learning handoff-prediction module and the dashboard's Research section were removed to keep the project focused on mobility mechanisms and their measurement. The campaign and statistics command-line tools remain.

## 8. What not to claim

- Do not say results are from a real network.
- Do not say Mobile IP here is RFC compliant, or that route optimisation is Mobile IPv6.
- Do not say one approach is "the best".
- Do not claim the dashboard shows live metrics. Metrics appear after a run ends.
- Do not claim campaign statistics appear in the dashboard. They are command-line only.
- Do not claim the live namespace mode works from the dashboard. It does not.

## 9. If something goes wrong

| Problem | Fix |
|---|---|
| Header says API unreachable | `make stack-stop && make stack-start`, wait a few seconds, reload |
| `Permission denied` running a script | `chmod +x scripts/*.sh`, then retry |
| QUIC migration is greyed out | `pip install -r requirements.txt`, then restart the stack |
| A run stays queued | Two jobs run at a time; wait, or check the **Active experiments** list |
| An experiment fails | Open it in **Experiment history** to read the error code and message in the lifecycle log, then run a new one |
| "An identical experiment is already queued or running" | Expected. Wait for the first to finish, or change one setting such as the seed |
| Compare page shows a warning | Expected when settings differ. Use it as a talking point |
| A page shows an error banner | Press the retry button; check that the API tab in the terminal is still running |
| Everything fails | Open the results you pre-ran earlier from **Experiment history**, and fall back on the screenshots in `docs/screenshots` |

## 10. One-page cheat sheet

**Start:** `make stack-start`, open `http://127.0.0.1:5173`. **Stop:** `make stack-stop`.

**Order of pages:** Overview, Network topology, Configure experiment, Experiment results, Compare experiments, Experiment history.

**Approaches (what to say in one line each):**

- TCP reconnection: the application repairs it, 100 ms with notification, 580 ms on timeout.
- Basic Mobile IP: the network repairs it, loses 16 probes.
- Make-before-break: same signalling, but service gap 20 ms and no loss.
- Route optimisation: extra signalling, 151 ms, more direct path afterwards.
- QUIC: the connection migrates, about 101 ms, no loss.

**Three sentences that show depth:**

1. "Handoff latency and service gap are different. Make-before-break proves it, same latency, much smaller gap."
2. "Reliable transports turn loss into delay, so loss is only comparable within one transport."
3. "Every result is reproducible from its stored configuration and seed, and anything simulated is labelled."
