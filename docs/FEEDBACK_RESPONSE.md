# Response to the review feedback

Three questions were raised: **(1)** why is there so much packet loss (TCP, Mobile IP, the others), how does it happen, how can it be
reduced, and where does ML fit; **(2)** trial aggregation: how many trials, and how are they combined; **(3)** why were specific
parameter values used. This document answers each from the code and from new experiments. Everything is **simulator-generated**
(virtual-time emulator); nothing here measures a real network.

New code: `backend/app/compare/loss_study.py`, `ml_study.py`, `ml_handoff.py`, `trial_study.py`, an opt-in change in `tcplite.py`, and
`backend/tests/test_tcp_fast_retransmit.py`. Raw results: `docs/studies/`.

---

## 1. Where the packet loss comes from

### 1.1 Mobile IP family (basic, route optimisation): loss = probe rate × blackout

The correspondent sends one probe every 10 ms (100 pps). At a handoff the mobile node's old link disappears. Until the Home Agent
has a **new binding** (movement detection 20 ms + registration request/reply round trip + processing), the Home Agent keeps
tunnelling probes to the **old care-of address**, and they are discarded (`link_down_in_flight`, and a routing loop that ends in
`ttl_expired` at the Home Agent because the old access router no longer has the connected route). Nothing else is lost.

Check on 284 simulated handoffs: lost packets vs measured interruption has slope **97.7 packets per second** (probe rate is 100) and
Pearson r = **0.998** (`loss_study.py blackout`). So: *loss ≈ rate × blackout*.

Route optimisation loses more (2.61 % vs 1.22 % at baseline) because the correspondent keeps sending to the **old** care-of address
until the new Binding Update is accepted, which needs the extra Return Routability exchange: a longer blackout, by design of the model.
Make-before-break loses 0 % at baseline because the old link stays up until the new registration completes.

### 1.2 The headline percentage is set by *how often* you hand off, not only by how bad each handoff is

| Setup (basic Mobile IP) | Loss |
|---|---|
| 12 s run, 2 handoffs | 1.39 % |
| 60 s run, 2 handoffs | 0.27 % |
| 60 s run, 10 handoffs | 1.34 % |
| probe rate 25 / 50 / 100 / 200 / 500 pps | 1.39 / 1.39 / 1.39 / 1.30 / 1.25 % |
| payload 64 B vs 1000 B | 1.39 % / 1.39 % |

Loss % = (handoffs per run × blackout) ÷ run length. The default run has a handoff every ~6 s, which is extremely mobile; a
percentage like 1.2 % does not describe a user who moves once a minute. Rate and payload do not matter for the percentage.

### 1.3 Even perfect movement detection does not remove it

Detection delay sweep for basic Mobile IP: 0 ms → 1.04 %, 20 ms → 1.39 %, 100 ms → 2.78 %, 250 ms → 5.39 %. The floor at 0 ms is the
registration round trip. Route optimisation floor: 2.35 %.

### 1.4 TCP: the "loss" is mostly a model artefact, now fixed

At baseline TCP loses 0 % (it retransmits; the cost shows up as stall time). Under 2 % random link loss the old model reported
**33 % median loss** (81 % at 5 %). These probes were never dropped on the wire in the way that number suggests: in a checked run,
probes 957 to 1149 were simply still **undelivered when the 12 s run ended**, queued behind TcpLite's retransmission timer.
Cause: TcpLite had no fast retransmit and its receiver discarded out-of-order data, so one lost segment froze the whole stream until a
(doubling) timeout, and each resend was exposed to loss again. Real TCP has fast retransmit (RFC 5681) and SACK.

Fix (opt-in, default unchanged, all previous tests and the byte-for-byte baseline still pass): 3 duplicate ACKs trigger an immediate
resend of the missing segment and the receiver buffers out-of-order data. Same seeds, 12 runs per cell, median (max):

| Condition | TCP notify, old model | TCP notify, with fast retransmit |
|---|---|---|
| baseline | 0 % loss, 105 ms stall | identical |
| 1 % link loss | 0 % (max 16.8 %), 234 ms | 0 %, 107 ms |
| 2 % link loss | **33.3 %** (max 62 %), 777 ms | **0 %**, 112 ms |
| 5 % link loss | **81.4 %** (max 90 %), 1477 ms | **0 %**, 210 ms |

TCP's remaining cost is stall time (reconnect + resume), not lost probes. TCP and UDP-probe loss are still **not like-for-like**
(reliable transports turn loss into delay), and every loss comparison carries that caveat.

### 1.5 How to reduce the real handoff loss (design levers, in order of effect here)

1. **Make-before-break** (0 % at baseline): needs two usable links.
2. **Shorter detection**: each 10 ms removed saves about 0.12 percentage points for basic Mobile IP.
3. **Anticipated registration** (register on the new link before the old one is lost): removes the registration round trip from the blackout.
4. **Home Agent buffering/bicasting** during the blackout, and for route optimisation **falling back to the home path** until the new binding is accepted.
5. **Reliable transport above the handoff** (TCP/QUIC) converts the remaining loss to delay.

Levers 3 and 4 are not implemented in the simulator; they are the natural next step. Do not claim them as results.

## 2. Where ML can honestly be used

ML cannot remove this loss, because it is a deterministic consequence of timing. Two uses are defensible, both implemented in
`ml_study.py` and evaluated on **conditions the model never saw** (5-fold cross-validation grouped by condition; 120 Latin-hypercube
condition vectors × 5 approaches = 600 runs):

* **Predict** loss and stall for unseen conditions (gradient-boosted trees).
* **Choose** the approach with the lowest predicted stall (adaptive policy), scored by regret against the true best.

| Result | Value |
|---|---|
| Loss: R² (model) / MAE vs predict-the-mean | 0.73 / 8.2 % vs 18.7 % |
| Stall: R² / MAE vs predict-the-mean | 0.39 / 340 ms vs 599 ms |
| Mobile IP stall MAE vs predict-the-mean | basic 401 vs 450 ms, route-opt 772 vs 863 ms |
| TCP loss MAE vs predict-the-mean | notify 10.5 vs 28.7 %, stall-timeout 7.7 vs 21.7 % |
| Selection among all approaches | 100 % hit, regret 0 ms, **but trivial**: always choosing make-before-break also has regret 0 |
| Selection without make-before-break | 68 % hit, regret **200 ms** vs 270 ms for the best fixed approach (always basic Mobile IP) |

Honest reading: the model is clearly useful for TCP (structured behaviour) and for loss overall, but for Mobile IP stall it barely beats
the mean, because most of the spread comes from **random loss and registration-loss draws**, which conditions alone cannot predict. The
adaptive policy gives a modest gain (about 26 % lower regret) only when make-before-break is unavailable. The TCP rows were generated with
the **old** TcpLite behaviour (fast retransmit off), so they partly learn the artefact in 1.4; regenerate with it on before quoting them.

### 2.1 ML that can actually reduce loss: predicting the handoff early (`ml_handoff.py`)

Reactive Mobile IP registers only after the old link is gone. If a model raises an alarm *before* the link is lost, registration can
start early and the blackout becomes max(0, reactive blackout − lead time). A false alarm costs one needless registration (signalling),
a missed event falls back to the reactive blackout.

**What is simulated:** a synthetic RSSI signal (two access points, log-distance path loss, AR(1) shadowing, noise, a mobile that mostly
walks from A to B and sometimes turns back; all radio constants illustrative) and an **analytic** blackout model fed with the emulator's
measured reactive blackout (85 ms basic Mobile IP, 160 ms route optimisation) and registration cost. The emulator itself cannot yet start
a registration before the old link drops, so this is **not** an emulator measurement; implementing an `anticipation_lead_ms` option in
`mobility_sim.py` and checking the formula against it is the remaining validation step.

Setup: 600 trajectories split 60/20/20 by trajectory; test = 120 trajectories, 109 handoffs, 17.5 minutes. Alarm threshold tuned on
validation for a false-alarm budget, then frozen. Compared: reactive, a one-line RSSI threshold rule tuned to the same budget, and a
gradient-boosted classifier. Lost packets per handoff (basic Mobile IP, reactive = 8.5):

| False-alarm budget | RSSI rule | ML classifier | Extra signalling (ML) |
|---|---|---|---|
| 0.3 / min | 8.5 (cannot operate) | **6.4** (2 missed) | 21 B/min |
| 1 / min | 4.1 | 3.6 | 110 B/min |
| 3 / min | 1.9 | 1.9 | 288 B/min |
| 10 / min | 3.0 (23 missed) | 1.9 | 343 B/min |

Honest reading: **anticipation** is what cuts the loss (about 8.5 → 1.9 packets per handoff, roughly 78 %, with ~70 % of handoffs losing
nothing, at about 2 to 3 false alarms per minute). **The ML model itself adds little over a one-line RSSI rule**: equal at 3 per minute,
slightly better at very strict budgets, where the rule cannot work at all. About 10 % of handoffs have no usable lead time in either
policy (sudden signal drops). Route optimisation shows the same pattern (16 → about 4.3 packets). Results come from one synthetic
signal model; real RSSI traces could change them in either direction.

## 3. Trial aggregation: how many, and how combined

**Aggregation rule used throughout:** (1) the two handoffs inside one run are averaged **first**, because they share that run's loss
draws and protocol state and are not independent trials; (2) the **median across runs** is reported with an exact distribution-free 95 %
confidence interval (needs n ≥ 6); the mean is only a convenience because outcomes are skewed and heavy-tailed; (3) approaches are compared
on matched replicate seeds (Wilcoxon signed-rank, Holm correction, bootstrap interval of the median difference).

**Why 12 (example) and 20 (standard):** a statistical floor, not a precision target. With 10 approach pairs, the smallest attainable
Holm-adjusted p-value at n = 12 is about 0.005, so significance is *possible*; at n = 6 it is not. The numbers were not tuned to a
required precision.

**What the trials actually vary.** Trials differ only in the handoff time (uniform 3.5 to 4.5 s, i.e. its phase against the 10 ms probe
grid) and the seeded loss draws. With no random loss the emulator is deterministic: baseline Mobile IP loss spans only 1.22 to 1.30 %,
and make-before-break is *identical* in all runs. Those narrow intervals reflect missing variability, not exact knowledge (flagged
`degenerate` in the analysis).

**How many are enough** (`trial_study.py`: 60 replicates, random subsets, target: median within ±10 % of the 60-run median in ≥ 95 % of
subsets; the ±10 % tolerance is a choice, state it when you quote this):

| Cell (2 % link loss unless noted) | Trials needed |
|---|---|
| Basic Mobile IP stall (baseline and 2 % loss) | 3 |
| Basic Mobile IP loss % | **10** |
| Make-before-break stall | **30** |
| Make-before-break loss % | **40** |

The classical t-based formula applied to a 12-run pilot asks for 255 trials (basic Mobile IP loss) and 1,101 (stall) because heavy tails
inflate the standard deviation; that is a reason to aggregate with the **median**, not a reason to run 1,000 trials. Conclusion: 12 is
adequate for most cells but **too few for make-before-break under loss (30 to 40 needed)**; use 20 to 40, or report that cell's interval as wide.

"How much is permitted" is not defined by the code or by a standard: it is whatever tolerance you choose to state in advance. Ask your
teacher which tolerance (for example ±10 %) she means; the table above shows the trial count for that choice.

## 4. Parameter provenance (which values are arbitrary)

| Parameter | Value | Basis | Effect on results |
|---|---|---|---|
| Link delays (CN-HA, HA-A, HA-B, A-MN, B-MN) | 5 / 10 / 20 / 5 / 15 ms | **Arbitrary**, illustrative; B is the slower "standby" path | Set handoff latency and RTT; scales with `latency_scale` |
| Link rate 50 / 10 Mbit, jitter 2 ms | as listed | **Arbitrary**; used only by the live `tc netem` lab, **ignored by the emulator** | None on emulated results |
| Probe rate 100 pps, payload 64 B | | **Arbitrary** | Loss % nearly independent (1.25 to 1.39 % over 25 to 500 pps); payload none |
| Run length 12 s, handoffs at ~4 s and ~7 s | | **Arbitrary** | Directly sets loss % (section 1.2) |
| Movement detection | 20 ms (100 ms sweep) | **Arbitrary**, no source in the repository | About 0.12 pt loss per 10 ms (section 1.3) |
| Random loss 1/2/5 %, registration loss 10/30/50 %, Home Agent +25..200 ms, latency ×0.5..4 | | **Arbitrary sweep levels**, plausible orders of magnitude, not calibrated | Define the scenarios |
| Mobile IP lifetime 30 s, route-opt lifetime 40 s, Home Agent processing 1 ms | | **Arbitrary** (educational) | None within a 12 s run |
| TCP initial RTO 1 s | RFC 6298 | **Standard** | |
| TCP min RTO 0.2 s | Linux default | **Standard-like** | |
| TCP max RTO 8 s, retry limit 6, SYN retries 5 | | **Arbitrary** (RFC 6298 allows a maximum of at least 60 s; Linux limits are larger, from memory, verify before quoting) | Shape TCP behaviour under heavy loss |
| Duplicate-ACK threshold 3 | RFC 5681 | **Standard** | |
| Application stall timeout 0.5 s, retry delay 0.1 s | | **Arbitrary application policy** | **Determines the "stall-timeout" TCP result**: stall ≈ 0.5 s timeout + watchdog quantisation + reconnect ≈ 636 ms |
| Replicates 12 / 20, α = 0.05, 95 % CI, 5,000 bootstrap resamples, Holm | | **Convention**; 12/20 justified in section 3 | |
| Base seed 20261028 | | Arbitrary, fixed for reproducibility | None systematically |

Not validated against any real network or standard trace. State this plainly when asked "why this value": most are *illustrative
defaults*, and the sensitivity columns show which ones actually move the conclusions (detection delay, run length/handoff rate, the
application stall timeout) and which do not (probe rate, payload, lifetimes).

## 5. Reproduce

```bash
PYTHONPATH=backend python3 -m app.compare.loss_study tcp-recovery --out-dir data/studies/tcp_recovery
PYTHONPATH=backend python3 -m app.compare.loss_study blackout --handoffs-csv data/experiments/example/analysis/handoffs.csv
PYTHONPATH=backend python3 -m app.compare.ml_study generate --out data/studies/ml_dataset.csv --n-conditions 120   # about 13 min on one core
PYTHONPATH=backend python3 -m app.compare.ml_study evaluate --data data/studies/ml_dataset.csv
PYTHONPATH=backend python3 -m app.compare.ml_handoff --out docs/studies/ml_handoff.json   # about 1 min
PYTHONPATH=backend python3 -m app.compare.trial_study run --out-dir data/studies/trials --replicates 60
PYTHONPATH=backend python3 -m app.compare analyze data/studies/trials
PYTHONPATH=backend python3 -m app.compare.trial_study report --out-dir data/studies/trials
```

Limits: one topology, one traffic load, two alternating handoffs, one-factor-at-a-time sweeps for the campaigns; QUIC migration was not
run (`aioquic` not installed in the analysis environment); the ML dataset has one random draw per (condition, approach).
