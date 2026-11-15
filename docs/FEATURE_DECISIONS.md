# Feature decision report

Purpose: record why each capability was kept, moved or simplified in the dashboard redesign, so the reasoning survives. Decisions: KEEP, KEEP AS ADVANCED, SIMPLIFY, REORGANISE, REMOVE. Status words come from the code and tests, not from this report. Update: at the project owner's request the machine-learning prediction module and the dashboard Research section were later removed (see the table).

| Feature | Current purpose | Research value | Implementation status | Dependencies | Decision | Justification | Affected components |
|---|---|---|---|---|---|---|---|
| Five-node topology (CN, HA, MN, ANA, ANB) | The fixed lab the experiments run on | Core: defines the experimental setup | Working; read from `config/topology.yaml` via `GET /api/topology` | `app.netem`, topology config | KEEP, SIMPLIFY view | One diagram first; node, link and route tables moved into collapsible sections | `TopologyPage`, `OverviewPage` |
| TCP reconnection (notification and timeout recovery) | Baseline with no mobility protocol | Core: shows what applications experience without help | Working, emulated (TcpLite model) and loopback (real TCP) | `app.compare.tcplite`, `loopback` | KEEP | Recovery behaviour stays a selectable option (`tcp_detect`) under one approach, matching the implementation | Configure form |
| Basic Mobile IP | Educational Mobile IPv4-style tunnelling | Core: signalling and triangular routing cost | Working, emulated; not RFC 5944 | `app.mobileip` | KEEP | Distinct handoff behaviour | none |
| Mobile IP make-before-break | Link overlap during handoff | Core: loss versus overhead trade-off | Working, emulated | `app.mobileip`, `compare` | KEEP | Kept separate from basic Mobile IP because handoff behaviour differs | Configure form shows handoff mode only for approaches that support it |
| Route optimisation model | Simplified Mobile IPv6 route optimisation | Core: path stretch | Working, emulated; labelled a simplified model | `app.mobileip.ro` | KEEP | Different routing behaviour | none |
| QUIC connection migration | Real aioquic migrating one connection | Core: transport-layer mobility | Runs when aioquic is installed. A 20 s run and a UI-started 12 s run completed with 0 lost probes; no campaign has been run | `aioquic`, `cryptography` | KEEP, fix availability | Was missing from fresh installs because the dependency was undeclared; now in `requirements.txt`. Still disabled with an install hint when the library is absent | `requirements.txt`, `approaches.py` |
| Probe traffic and measurement | Sequence-numbered UDP probes; loss, gap, RTT, goodput | Core | Working, tested | `app.traffic` | KEEP | Backend remains the only source of measurements; the frontend computes none | Results page |
| Emulated, loopback and live environments | Where a run executes | Core: provenance | Emulated and loopback work; live is CLI-only and unverified | `app.netem` | KEEP, SIMPLIFY | Environment moved under Advanced settings; every result stays labelled simulated and not live | Configure form, badges |
| Experiment jobs (queue, workers, cancel, idempotency, SQLite) | Reliable background execution | Supporting: reproducibility and safety | Working, tested | `app.services` | KEEP | Invisible to users but required for integrity | none |
| Results page | Show one run | Core | Working | metrics, handoffs, events endpoints | SIMPLIFY, REORGANISE | Five headline values and two charts first; additional metrics, raw handoff table, event timeline, configuration and lifecycle log under Advanced | `LivePage` |
| Progress and status polling | Show job lifecycle | Supporting | Working | `usePolledExperiment` | KEEP | Shown only while a run is active; completed runs no longer repeat it | `LivePage` |
| Comparison | Side-by-side runs | Core | Working | metrics endpoint | KEEP, REORGANISE | Condition-mismatch warning stays; the conditions table is collapsible and opens when conditions differ; no winner is ever declared | `ComparePage` |
| History, filters, exports | Browse and export runs | Core: reproducibility | Working | history endpoints | KEEP | Unchanged apart from styling | `HistoryPage` |
| Campaigns and statistics | Replicated runs, confidence intervals, Wilcoxon, Holm, bootstrap | Advanced: defensible comparison | Working, tested; CLI only, no API endpoint | `app.compare.campaign`, `stats` | KEEP (command line only) | The dashboard Research page was removed at the owner's request; the command-line workflow is documented in `docs/FEATURES.md` | `docs/EXPERIMENTS.md` |
| Handoff prediction and model evaluation | Threshold, logistic regression and random forest on simulated data | Separate research question | Worked; needed scikit-learn 1.8.x | `app.ml` | **REMOVED (owner decision)** | Removed to keep the project focused on mobility mechanisms. Lost: the prediction study, its model bundle, API routes (`/models`, `/predictions`), the `predict` request fields and the dashboard pages. Existing saved experiment data is untouched | Backend `ml/`, services, API, frontend, tests, docs |
| CLI tools | Batch runs, analysis, reproduction | Advanced | Working | `app.*` | KEEP | Not forced into the browser | none |
| Theme and layout | Presentation | None | New | CSS tokens | NEW | Sidebar navigation, light and dark themes, responsive menu | `styles.css`, `App.jsx`, `useTheme.js` |

## Removed or merged
- Live-metrics wording: the page is now "Experiment results" (route `#/live/<id>` is unchanged so old links work). Nothing is measured live.
- The duplicated status block and the always-visible lifecycle table on finished runs. The data is still available under Advanced.

## Not done
- No campaign API or browser view of campaign results (campaigns are command line only).
- "Advanced metrics" is not a separate page; those values sit under Advanced on the results page.
