# Experiment and analysis framework

This document describes how to run replicated comparison experiments, how the results are analysed, what the numbers mean, and how
to reproduce a reported finding. The framework **extends** the comparison runner (`app/compare`): campaigns call
`run_experiment(ExperimentSpec)` for every run; there is no second runner.

> Everything in the shipped results was produced by the deterministic **virtual-time emulator** . It is
> *simulator-generated* data. Nothing here is a measurement of a real network, and no result may be read as "approach X is better on
> real networks".

## 1. What is new

| Area | Change | Module |
|---|---|---|
| Conditions | `NetworkConditions`: latency scale / per-link delay, Home Agent distance, random packet loss, registration loss, Home Agent processing time, seed | `app/netem/conditions.py` |
| Emulator | optional **seeded random loss** per link and direction (off by default: no random number is drawn, default-condition results are byte-identical) | `app/mobileip/simnet.py` |
| Spec | `ExperimentSpec.seed`, `ExperimentSpec.conditions`, `ExperimentSpec.from_json` | `app/compare/experiment.py` |
| Provenance | `experiment.json` now also records library versions, effective link delays/loss and which conditions apply to the approach | `app/compare/runner.py` |
| Campaigns | design, resumable execution, append-only ledger, failure/interrupt recording | `app/compare/campaign.py` |
| Statistics | exact median CI, bootstrap, matched-pairs tests, Holm, sample size | `app/compare/stats.py` |
| Analysis | run table, handoff table, summaries, comparisons, CSV/JSON/Markdown | `app/compare/analysis.py` |
| Charts | PNG charts from saved analysis | `app/compare/plots.py` |
| Reproduction | re-run saved runs and compare byte-for-byte | `app/compare/reproduce.py` |

### A defect found and fixed on the way (TcpLite)

`tcplite.py` documented "go-back-N via RTO" but retransmitted **only the head segment** on a timeout, while its receiver discards
out-of-order data. Without random loss this never mattered; with 0.5 % loss delivery collapsed (52 % of probes never arrived, RTO
backed off to its maximum) — a model artefact, not a TCP property. The timeout now resends all outstanding segments, as documented.
Check performed: the `matrix` command was run before and after; all handoff/loss/RTT metrics and raw CSVs are byte-identical, only
the internal TcpLite counters of the `tcp-reconnect/stall-timeout` run changed (segments, bytes and send failures on the dead old
connection: 2024→2069, 154652→159557, 4→10). **TcpLite still has no fast retransmit, SACK or congestion control**, so its results under
loss (especially ≥ 5 %) describe the model and are flagged `model_limit_note` in every export.

## 2. Commands

All commands run from the repository root; `PYTHONPATH=backend` as in the Makefile (shown as `make` targets and as raw commands).

```bash
# what exists, and where it can run (discovered from app/compare/approaches.py, nothing hard-coded)
PYTHONPATH=backend python3 -m app.compare list --json

# show the design of a campaign without running anything
PYTHONPATH=backend python3 -m app.compare plan --config config/experiments/example.yaml

# run (or resume) a campaign, then analyse and plot it
PYTHONPATH=backend python3 -m app.compare campaign --config config/experiments/example.yaml \
        --out-dir data/experiments/example      # make exp-example

# analyse EXISTING data (no experiment is run); also accepts older folders such as `compare matrix` output
PYTHONPATH=backend python3 -m app.compare analyze data/experiments/example                    # make exp-analyze DIR=...

# charts from the saved analysis
PYTHONPATH=backend python3 -m app.compare plots data/experiments/example                      # make exp-plots DIR=...

# reproduce reported results: re-run saved runs from plan.json and compare byte-for-byte (exit status 1 on mismatch)
PYTHONPATH=backend python3 -m app.compare reproduce data/experiments/example --sample 10      # --sample 0 = all runs

# one run with explicit conditions
PYTHONPATH=backend python3 -m app.compare run --approach mip-basic --loss-percent 2 --ha-distance-ms 100 --seed 7 --out-dir /tmp/r

# replicates needed for a given precision, from a pilot standard deviation
PYTHONPATH=backend python3 -m app.compare sample-size --sd 12 --halfwidth 3
```

Interrupting a campaign (Ctrl-C or SIGTERM) records the interrupted run and exits with status 130; **rerun the same command to
resume**. `--max-runs N` stops cleanly after N new runs; `--retry-failed` reruns runs recorded as failed (failures are otherwise
deterministic and kept as they are). A directory belongs to one configuration (SHA-256 recorded in `campaign.json`); another
configuration needs another directory.

## 3. Experimental design

* **Approach configurations** come from the registry: `tcp-reconnect` (two variants: `notify`, `stall-timeout`), `mip-basic`,
  `mip-mbb`, `ro-model`, `quic-migration`. Cells that cannot run are **listed with the reason, never dropped**: `unavailable`
  (e.g. `aioquic` not installed), `unsupported`, or `not_applicable`
  (a Home Agent distance cannot affect TCP, which never uses the Home Agent).
* **Conditions**: one baseline plus one-factor-at-a-time sweeps:

  | factor | meaning |
  |---|---|
  | `latency_scale` | multiplies every one-way link delay |
  | `loss_percent` | independent random loss per direction on both access links (`ana-mn`, `anb-mn`), applied to all packets (data and signalling) |
  | `ha_distance_ms` | extra one-way delay on every link touching the Home Agent (`cn-ha`, `ha-ana`, `ha-anb`) |
  | `detect_delay_ms` | movement-detection delay (handoff timing) |
  | `dwell_s` | time between the two handoffs (handoff timing) |
  | `registration_loss_percent` | loss of Mobile IP registration requests/replies only |
  | `ha_processing_ms` | Home Agent registration processing time |
  | `old_link_hold_ms` | make-before-break old-link hold (`mip-mbb` only) |

  Interactions between factors are **not** estimated.
* **Replicates and seeds.** Replicate *k* draws its handoff schedule (first handoff ~ U(3.5, 4.5) s, second after `dwell_s` ±0.25 s)
  from `SeedSequence([base_seed, k, 2])` and its loss seed from `SeedSequence([base_seed, k, 1])`. These do **not** depend on the
  approach or the condition: every approach and every condition sees the same 12 schedules (*common random numbers*). That makes
  the comparisons matched pairs and removes schedule noise from them. Without the schedule jitter a deterministic emulator would
  return identical replicates (some cells still do — see "degenerate" below).
* **Traffic**: identical in every run (UDP-style probes at 100 pps, 64 B payload, 12 s) unless the config changes it; deviations are
  recorded in the run's `experiment.json`/`runs.csv` (`p_*` columns).

## 4. Metrics and provenance

Each metric carries a provenance label in `summary.csv` and a definition in `metric_definitions.json`.

* **simulator-generated; directly observed in the raw records** — timed or counted from the run's own packet/event records.
* **simulator-generated; derived** — computed with a reference or label that is not a measurement of that run.
* "measured on real kernel sockets" would be used for loopback/live runs; none are part of the shipped results.

| metric | kind | notes |
|---|---|---|
| `app_stall_ms` | observed | longest echo-arrival gap around a handoff — the application-visible stall, defined for every approach |
| `interruption_ms` | observed | loss-burst interruption; only where a probe-loss burst exists (reliable transports have none) |
| `handoff_latency_ms` | observed | to the approach's **own** recovery point; **not compared across approaches** |
| `loss_percent`, `handoff_loss_packets` | observed | reliable transports hide loss as delay: comparable only within a transport class (caveat is written into every loss comparison) |
| `handoffs_unrecovered` | observed | handoffs that never recovered (visible failures under heavy registration loss) |
| `signalling_messages/bytes_per_handoff`, `mobility_overhead_bytes_per_packet` | observed | counted from simulated byte-accurate packets / events (overhead is `derived` if its basis is analytic) |
| `path_stretch` | **derived** | observed RTT after the handoff ÷ direct-path RTT computed from the run's effective link delays |

## 5. Statistics

* **Unit of analysis = one run.** Handoffs inside a run share its loss realisation and protocol state; they are averaged within the run
  and only the run-level value enters any interval or test. `handoffs.csv` is exported for transparency but never used for inference.
* **Median and its CI**: exact distribution-free interval from order statistics (binomial(n, ½)); coverage ≥ 95 %, the achieved coverage
  is exported (`median_ci_coverage`). It needs n ≥ 6; below that no interval is produced (and none is invented).
* **Mean CI**: Student-t (convenience; assumes an approximately normal mean).
* **Comparing two approaches** (same condition, same replicate seeds): paired difference *d = a − b*; effect = median of *d* with a seeded
  percentile-bootstrap CI over pairs (5000 resamples); two-sided Wilcoxon signed-rank test; `prob_a_lower`. Holm–Bonferroni within each
  (condition, metric) family of approach pairs (and within each (configuration, metric) family for condition-vs-baseline).
* **Verdict**: "a lower"/"b lower" only if Holm-adjusted p < 0.05 **and** the CI of the median difference excludes 0 **and** n_pairs ≥ 6;
  otherwise "no distinguishable difference" or "insufficient data". The wording is directional per metric and per condition. There is
  no overall ranking and the word "superior" is not used.
* **Degenerate cells**: if all replicates are identical (the emulator did not vary), the interval has zero width; this is flagged
  (`degenerate`, `ci_note`) because it reflects absent variability, not exact knowledge.
* **Survivorship**: statistics use finished (`ok`) runs; counts of failed/incomplete/not-run runs sit beside every summary row.
* **Sample size**: the exact median CI needs n ≥ 6; the exact two-sided Wilcoxon p-value cannot go below 2/2ⁿ, and Holm over the 15
  pairs of 6 configurations needs p < 0.05/15 ≈ 0.0033, i.e. n ≥ 10. The example campaign uses n = 12 (smallest attainable
  Holm-adjusted p ≈ 0.007), the standard campaign n = 20. For a precision target use `sample-size` with a pilot standard deviation
  (a derived estimate). All CIs describe sampling variability *of the simulator under this design*.

## 6. Persistence and failure handling

```
<campaign>/campaign.json   config + SHA-256 + software versions        plan.json   every planned run with full spec/seeds
<campaign>/ledger.jsonl    append-only start/end records (fsync per record)
<campaign>/runs/<scenario>/<run>/    sent/echoes/received/events/approach_events CSVs, experiment.json, comparison_metrics.json,
                                     campaign_run.json, traceback.txt (failures)
<campaign>/_attempts/      earlier failed/interrupted/crashed attempts, moved aside, never deleted
<campaign>/analysis/       runs/handoffs/summary/comparisons/vs_baseline (.csv/.json), REPORT.md, plots/
```

* `failed` — exception inside a run: traceback and experiment.json kept. `interrupted` — Ctrl-C/SIGTERM: partial raw files kept and
  listed. `incomplete` — ledger start without an end (crash, kill -9, power loss), or "ok" without its metrics file. `not_run` — planned
  but never started. All appear in `runs.csv` with their reason and are counted in `summary.csv`.
* `analyze` reads only these files, never runs a simulation, contains no absolute paths and is seeded: regenerated outputs are
  byte-identical except the timestamp in `analysis_meta.json` (tested).

## 7. Reproducing reported findings

1. `make exp-example` (or the `campaign` command above) regenerates every raw run and the analysis from `config/experiments/example.yaml`
   (same `base_seed`, same code → identical raw files).
2. `python -m app.compare reproduce data/experiments/example --sample 0` re-runs **every** saved run from `plan.json` and compares
   six raw/metric files per run byte-for-byte.
3. `python -m app.compare analyze data/experiments/example` regenerates all tables, `REPORT.md` and, with `plots`, all charts from the saved runs.
   The statements in `data/experiments/example/analysis/REPORT.md` come from `comparisons.csv`/`vs_baseline.csv`.

## 8. Limitations

* Fixed link delays (no jitter, queueing or bandwidth limits in the emulator); loss is independent per packet and direction.
* TcpLite simplifications (see §1); the QUIC adapter only runs when `aioquic` is installed (it is in `requirements.txt`); `live`/namespace environments are not part of campaigns.
* One topology, one traffic load, one handoff pattern (two alternating handoffs); one-factor-at-a-time sweeps only.
* Replicates vary the seeded handoff schedule and loss draws; they do not vary protocol parameters or the topology.
* The reference numbers below come from the 12-replicate example campaign; the 20-replicate standard campaign is defined in `config/experiments/standard.yaml`.

## 9. Reference results of the example campaign

These are the values produced by `make exp-example` (the campaign is deterministic for a given seed and code version).

Campaign `example`: 372 runs (12 replicates × 31 cells), all finished (`ok`), emulated environment, 12 s runs, 100 pps probes.
In the reference run QUIC was **not measured** (`aioquic` was not installed there → reported as `unavailable` in `not_executed_cells.csv`);
Home-Agent factors skip TCP/QUIC (`not_applicable`). The full tables are in `analysis/`; `analysis/REPORT.md` is generated from them.
Everything below is *simulator-generated*; intervals are exact 95 % CIs of the median over 12 independent runs.

**Baseline application stall** (median [CI], ms): `mip-mbb` 20 (identical in all runs), `mip-basic` 80 [80, 85], `tcp-reconnect-notify`
104.8 [104, 106], `ro-model` 160 [160, 165], `tcp-reconnect-stall-timeout` 636 [610, 658]. All 10 pairwise differences are distinguishable
(12/12 matched pairs in the same direction, Holm-adjusted p = 0.0049 — the smallest value attainable with n = 12 and 10 pairs). "Lower stall"
here means exactly that, for this metric, in this simulator and these conditions; it is not an overall ranking (see signalling cost and
path stretch next).

**Other metrics.** Handoff latency to each approach's own recovery point, signalling bytes (`mip-*` 120, `tcp` 138, `ro-model` 202 per handoff)
and path stretch (`mip-basic`/`mip-mbb` 1.25, others 1.00 at baseline) are *identical in every replicate*: they are reported as deterministic
differences, without p-values. Probe loss at baseline: `mip-basic` 1.22 %, `ro-model` 2.61 %, `mip-mbb` and both TCP variants 0 % — TCP
retransmits, so this is not like-for-like (flagged in every loss comparison).

**Effect of conditions (same approach vs baseline, matched pairs, Holm):**
* Home Agent +100 ms: `mip-basic` stall 80 → 170 ms (+90), handoff latency +200 ms and path stretch 1.25 → 6.95 (deterministic); `ro-model` stall
  160 → 252; `mip-mbb` stall unchanged at 20. After route optimisation `ro-model` path stretch stays 1.00.
* All link delays ×2: every approach's stall increases (e.g. `mip-basic` 130, `ro-model` 295, `tcp-notify` 186).
* 2 % loss on the access links: `mip-basic` 82, `mip-mbb` 22, `ro-model` 165 ms (hardly changed). `tcp-reconnect` medians rise to 776 [252, 940]
  and 654 [638, 958] ms, **but TcpLite has no fast retransmit/SACK, so this describes the model, not TCP**. At least one `ro-model` run had a handoff that never recovered.
* 30 % registration-message loss: heavy tails — `mip-basic` 85 [80, 580], `ro-model` 665 [165, 2810]; the worst runs contain up to 1–2 handoffs that never
  recovered (`handoffs_unrecovered`, reported, not hidden). `mip-mbb`'s application stall stays 20 ms because its old link stays up, even though
  a handoff did not complete in at least one run.
* Movement-detection delay 100 ms and a 1.5 s dwell: detection delay lengthens the break-before-make stalls (`mip-basic` 160, `ro-model` 240); the dwell change does not alter the medians.
