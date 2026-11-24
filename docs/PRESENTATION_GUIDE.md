# Presentation guide: changes made and how to present them

All numbers are from the simulator (virtual-time emulator). Nothing was measured on a real network. Say this once, early.

## 1. Everything that changed, from the start

| # | Change | Where | Answers her question |
|---|---|---|---|
| 1 | Loss analysis: loss = probe rate x blackout (284 handoffs, slope 97.7/s, r = 0.998) | `compare/loss_study.py` | Why so much loss, how it happens |
| 2 | TCP fix: opt-in fast retransmit (3 dup-ACKs) and out-of-order buffering. Default unchanged. 2 % link loss: 33 % -> 0 % loss | `compare/tcplite.py`, `tests/test_tcp_fast_retransmit.py` | TCP packet loss |
| 3 | Sensitivity sweeps: handoff rate, run length, detection delay, probe rate, payload | `loss_study.py`, section 1.2-1.3 of FEEDBACK_RESPONSE.md | Why the percentage is what it is |
| 4 | ML prediction and approach selection on unseen conditions (600 runs) | `compare/ml_study.py` | Integrate ML (modest result) |
| 5 | ML handoff anticipation: predict the handoff early, loss 8.5 -> about 1.9 packets per handoff | `compare/ml_handoff.py`, `tests/test_ml_handoff.py` | Integrate ML (the part that reduces loss) |
| 6 | Trial-count study: how many runs are enough (3 / 10 / 30 / 40 by cell) | `compare/trial_study.py` | Trial aggregation, how much is permitted |
| 7 | Parameter provenance table: which values are arbitrary and which matter | FEEDBACK_RESPONSE.md section 4 | Why these values |
| 8 | **UI: Handoff mode dropdown removed.** Mobile IP make-before-break is always sent as `mbb`; summary shows "make-before-break (fixed)" | `frontend/src/pages/ConfigurePage.jsx`, `lib/validation.js`, one new test in `lib/logic.test.js` | Your request |

Only the make-before-break approach ever offered that dropdown (`MBB_CAPABLE = {"mip-mbb"}`), so other approaches are unaffected. The backend still accepts `handoff_mode`, so the API and old saved experiments are unchanged.

## 2. Talk track (about 8 minutes)

1. **Open (30 s).** "You asked three things: why the loss, how trials are aggregated, and why these values. I checked each against the code and ran new experiments. Everything is simulated."
2. **Loss (2 min).** Mobile IP loss is probe rate x blackout, verified on 284 handoffs. The headline percent depends on handoff frequency (1.39 % with 2 handoffs in 12 s, 0.27 % with 2 in 60 s). Make-before-break loses 0 % because the old link stays up.
3. **TCP (1 min).** "The 33 % was a flaw in my TCP model: no fast retransmit, so probes sat behind the timer when the run ended. With standard fast retransmit it is 0 % and baseline results do not change."
4. **ML (2 min).** "ML cannot remove timing-based loss. I used it two ways. Predicting handoffs early cuts loss from 8.5 to about 1.9 packets per handoff, but a one-line signal-strength rule does almost as well, so the gain comes from anticipation, not from ML. This uses a synthetic signal and an analytic blackout formula, not an emulator run."
5. **Trials (1 min).** Handoffs in a run are averaged first, then the median across runs with a 95 % CI. 12 runs is enough for most cells; make-before-break under loss needs 30 to 40. "Which tolerance did you mean by 'permitted'? I used plus or minus 10 %."
6. **Parameters (1 min).** Most are illustrative defaults. The ones that move results are detection delay, run length / handoff rate and the 0.5 s application timeout; probe rate and payload do not.
7. **Limits (30 s).** One topology, synthetic RSSI, QUIC not run, ML TCP rows come from the old TCP behaviour.

## 3. Live demo (about 5 minutes)

Before: start backend and frontend, open `http://127.0.0.1:5173/#/configure`, and have one finished run ready in History as a fallback.

1. **Configure page, the new change.** Select "Mobile IP make-before-break". Point out there is no handoff mode field any more, and the summary panel says "make-before-break (fixed)". Select "Mobile IP (basic)" to show it reads break-before-make.
2. **Run basic Mobile IP**, baseline scenario, then **make-before-break** with the same seed. Open Compare: loss about 1.2 % versus 0 %. This is the concrete picture of "loss = blackout".
3. **TCP fix (optional, 4 min run, so run it before the meeting):**
   `PYTHONPATH=backend python3 -m app.compare.loss_study tcp-recovery --out-dir data/studies/tcp_recovery`
   Show the with/without fast retransmit loss.
4. **Show the documents:** FEEDBACK_RESPONSE.md section 1.4 (TCP), section 2.1 (ML), section 4 (parameter table).

Pre-demo check: `PYTHONPATH=backend python3 -m unittest backend.tests.test_tcp_fast_retransmit backend.tests.test_ml_handoff backend.tests.test_compare_tcplite` (19 tests pass) and `cd frontend && npx vitest run`.

## 4. Likely questions

* **"Why not just use ML to fix loss?"** The loss is a consequence of registration timing. ML can only help by predicting the handoff early, and I measured that a simple rule gets most of the benefit.
* **"Is this validated on a real network?"** No. Simulator only; the RSSI model and most parameters are illustrative.
* **"Why 12 runs?"** A statistical floor for significance tests, not a precision target. For a +/-10 % tolerance, 3 to 40 runs are needed depending on the cell.
* **"Why did you remove the handoff mode?"** Make-before-break is the point of that approach; a break-before-make option on it duplicated basic Mobile IP.

## 5. Open items to be honest about

* `anticipation_lead_ms` is not implemented in the emulator, so the ML blackout reduction is a formula fed with measured numbers.
* The first ML dataset's TCP rows predate the TCP fix; do not quote them.
* "Unverified cause": the routing-loop-at-old-router explanation for some drops is a reading of the code, not tested.
