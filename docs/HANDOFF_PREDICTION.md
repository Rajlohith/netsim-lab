# Stage 6: handoff-degradation prediction

Code: `backend/app/ml/`. Tests: `backend/tests/test_ml_*.py`. Shipped, reproducible artifacts: `examples/ml/stage6/`.
**Everything here uses SIMULATED network measurements** (the Stage 4/5 discrete-event emulator). No feature is, or approximates,
physical radio signal strength; the simulator has no radio model.

## 1. What was found in the existing project (and what that means for ML)
* Runs (Stage 3-5) already contain `sent.csv`, `echoes.csv`, `events.csv` (`handoff_start`/`handoff_end`) and `approach_events.csv`. The ML code **reads these files; it
  does not re-run or alter simulation logic**. Training data is produced by calling the existing `compare.run_experiment` (`python -m app.ml generate`).
* The emulator is **deterministic**: fixed link delays, no random loss, no jitter (the `jitter_ms` config key is not used by `simnet`). A handoff is an abrupt
  link change at a scheduled time. **The measurements therefore contain no gradual pre-handoff degradation**: RTT, loss and jitter are flat until the link changes.
  A model can only gain skill from (a) regularities in how the experiment generator schedules moves (dwell times), seen through handoff history or through RTT
  transients that reveal "a handoff just happened", or (b) nothing. Section 7 shows which of these happened.

## 2. Feature availability (`python -m app.ml features`)
Available (computed from observations with timestamp <= t only): RTT (last, short/long mean, max, std, trend, least-squares slope), RTT-delta jitter,
probe-loss rate (short/long window, probes younger than 250 ms are "pending", not lost), time since last echo and its ratio to the probe interval,
delivered throughput (goodput of echoes), retransmission / timeout-failure counts from `approach_events.csv`, handoff history (count in last 30 s, total,
time since last start/end, last duration, in-progress flag). Full definitions: `examples/ml/stage6/model/feature_schema.json`.

Marked unavailable (never fabricated, listed in the schema): signal strength / SNR / link-quality indicator, position or speed, queue length, bandwidth estimate,
TCP retransmissions for the TCP approach (TcpLite keeps a counter but the adapter does not log it, so `retransmit_count_long` is NaN for `tcp-reconnect`;
only the Mobile-IP family logs retransmissions), one-way delay (needs receiver clocks; deliberately unused), QUIC signals (adapter never run, K1).
Missing values are NaN in the dataset; models impute the **training** median and add missing indicators.

## 3. Target and labelling (exactly reproducible; `labels.py`, parameters in `config.py`)
* **Degradation episode** = a handoff (`handoff_start` in `events.csv`) accompanied by a service gap: an interval between two consecutive *echo arrivals* of at least
  `max(60 ms, 3 x probe interval)` that ends after the handoff starts and begins before `handoff_end + 250 ms`. Gaps rather than lost sequence numbers are used because
  the TCP approach delivers every probe late (no loss) but still stalls. A gap with no handoff is reported in the dataset metadata and never labelled. Make-before-break
  handoffs with no gap are "benign" handoffs: no episode, so no positives.
* **Onset** = the `handoff_start` instant. **Horizon** = 1.0 s.
* **Decision times**: first probe + 2 s, then every 0.25 s.
* **label(t) = 1** iff an episode onset lies in (t, t + 1 s]. Rows with `onset <= t <= episode end` are *excluded* (degradation already under way), and rows whose
  horizon passes the end of the run are *excluded* (label unknowable). Exclusion counts are in every results file.
* Labels use the future; **features never do**. `tests/test_ml_features.py` recomputes features on a copy of the run physically truncated at t and requires identical values;
  `extract_features` also raises `LeakageError` if it ever touches an observation newer than t.

## 4. Models (`models.py`)
1. **Threshold baseline**: single-feature rule `alarm iff feature >= x` on reactive symptoms (RTT trend/slope, jitter, loss, overdue echoes, retransmit/failure counts); the
   feature and x with the best training F1. Thresholds must lie strictly above the steady-state level, otherwise the rule degenerates into "always alarm".
2. **Logistic regression** (median imputer + indicators, standardisation, `class_weight=balanced`).
3. **Random forest** (200 trees, `min_samples_leaf=5`, `class_weight=balanced_subsample`).
Hyper-parameters are fixed (no tuning on held-out data). The alarm threshold of LR/RF is chosen on the validation split (max F1).

## 5. Data and splitting
`python -m app.ml generate` runs 120 experiments (40 scenario families x 3 movement schedules, 40 s each): approaches TCP reconnect (event / timeout detection), basic Mobile IP,
Mobile IP make-before-break, route-optimisation model; probe rate 50/100 pps; detection delay 20/100 ms; dwell model `uniform` U(2.5, 6) s or `exponential` (1.5 s + Exp(2 s)).
QUIC migration is not generated (aioquic not installed; reported as unavailable). Only the parameters (move schedule, rate, delay) are chosen by the generator; every measurement
comes from the emulator. Raw runs (~100 MB) are regenerated by `make ml-generate` (deterministic, resumable) and are not shipped; the processed dataset is.

**Split by scenario family** (all runs of a family go to one split), stratified by dwell model so every split sees both; 24 / 8 / 8 families. `splits.assert_no_leakage` checks disjointness of
families and experiments and is tested (a random row split is shown to be caught). The seed search only looks at label counts, never at model scores.
**Class imbalance** (included decision points): overall 3431 positive / 14092 negative (19.6 % positive, about 1 : 4.1); train 18.4 %, validation 18.6 %, test 24.1 % positive. Balanced
class weights are used in training; metrics are reported at an operating point and next to an "always alarm" reference (F1 = 2p/(1+p)).

## 6. Evaluation
Held-out test families only: precision, recall, F1, false-alarm rate FPR = FP/(FP+TN), false-alarm *events* per simulated hour (runs of consecutive false alarms count once),
average precision, ROC-AUC, warning time (lead of the first alarm inside the labelled horizon; per-episode, bounded by the 1 s horizon and quantised to 0.25 s), and 95 % bootstrap
intervals obtained by resampling whole experiments. Ablation: the same models without the handoff-history features. Breakdown by scenario, approach, and dwell model.

## 7. Results (run of 2026 on the shipped dataset; `examples/ml/stage6/RESULTS.md`, `results.json`)
Held-out test (8 families, 3537 decision points, 215 assessable episodes):

| model | precision | recall | F1 | false-alarm rate | episodes warned |
|---|---|---|---|---|---|
| always alarm (reference) | 0.241 | 1.000 | 0.388 | 1.000 | - |
| threshold baseline (`loss_rate_long >= 0.004`) | 0.221 | 0.597 | 0.323 | 0.666 | 134/215 |
| logistic regression | 0.426 | 0.447 | 0.436 | 0.191 | 117/215 |
| random forest | 0.386 | 0.543 | 0.452 | 0.274 | 146/215 |
| logreg, measurements only | 0.276 | 0.630 | 0.384 | 0.524 | 151/215 |
| random forest, measurements only | 0.291 | 0.912 | 0.441 | 0.704 | 204/215 |

Interpretation, limited to what the numbers support:
* The threshold baseline is **below** the always-alarm F1: reactive symptoms do not precede handoffs in this emulator (as expected from section 1).
* LR and RF are modestly above always-alarm, with wide intervals (RF F1 95 % CI 0.42-0.49; LR 0.38-0.49). Alarm rates stay high (FPR 0.19-0.27); this is **not** a
  usable early-warning system.
* The dwell-model breakdown shows where the skill comes from: on `uniform`-dwell families F1 is 0.50 (LR) / 0.48 (RF) vs always-alarm 0.37; on `exponential`-dwell families F1 is 0.37 (LR) /
  0.42 (RF) vs always-alarm 0.41, i.e. no clear skill. The skill that exists is a property of how the generator schedules moves, not an anticipation of degradation from network symptoms.
  Measurements-only models keep some of it because RTT transients after a handoff reveal "a handoff just happened".
* Warning times (median ~0.8 s) are close to the 1 s ceiling because the models alarm frequently; they are not evidence of early detection.

## 8. Using it
```
make ml-features                       # schema and unavailable features
make ml-generate && make ml-pipeline   # or: python -m app.ml generate / pipeline (see --help)
PYTHONPATH=backend python -m app.ml pipeline --raw-dir examples/comparison --out-dir /tmp/ml   # too little data -> status "pending", no model
PYTHONPATH=backend python -m app.ml predict data/results/mip-basic-emulated/run-1 --model-dir examples/ml/stage6/model
PYTHONPATH=backend python -m app.compare run --approach mip-basic --duration 40 --predict-model examples/ml/stage6/model
```
`predict` / `--predict-model` write `handoff_predictions.csv/json` next to the run's raw files (nothing existing is modified). The JSON says whether the run belongs to the model's
training split (then it is not a held-out evaluation). Python API: `load_bundle`, `HandoffPredictor.predict_at(run, t_ns)` (returns decision time, horizon end, score, alarm, newest observation used).
Model bundle (`examples/ml/stage6/model/`): estimators, `feature_schema.json`, `preprocessing.json`, `label_definition.json`, `thresholds.json`, `metrics.json`, `training_metadata.json`
(model version, library versions, seed, dataset hash, split membership), `MANIFEST.json` (sha256). `.joblib` files are pickles: load only bundles you trust; the loader rejects hash,
schema or scikit-learn major.minor mismatches.

## 9. Limitations
* Simulated, deterministic data from one emulator; no radio signal strength, no random loss/jitter, abrupt handoffs. Results do not transfer to real networks.
* The target depends on the labelling parameters (60 ms gap, 1 s horizon, 0.25 s step). Different values give different numbers.
* Skill is tied to the experiment generator's dwell-time model (section 7). Only 8 families are held out; split choice matters (bootstrap intervals show it).
* The predictor needs probe results and handoff events in one place (the probe sender side plus the orchestrator's event log).
* QUIC migration is excluded; the TCP approach has no retransmission feature; `mip-mbb` produces almost no episodes (benign handoffs).
* Not tested: real (live namespace) runs, the FastAPI layer, any dashboard integration. Model files depend on the scikit-learn version.
