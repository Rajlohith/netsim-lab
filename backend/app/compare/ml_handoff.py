"""ML handoff anticipation: can a model that predicts the loss of the serving link EARLY cut the handoff blackout?
(SIMULATED radio signal + numbers taken from the emulator; not a measurement of any real network.)

Idea. Reactive Mobile IP starts registering only after the old link is gone, so every probe sent during detection + registration is
lost (lost ~= rate x blackout, verified r = 0.998 in loss_study.py). If a predictor raises an alarm `lead` ms BEFORE the link is
lost, registration can start early and the blackout shrinks to max(0, reactive_blackout - lead). A false alarm costs one needless
registration (signalling) but no loss. A missed event falls back to the reactive blackout.

What is simulated and what is not (be explicit when quoting):
  * SIMULATED: a synthetic RSSI trace per trajectory (log-distance path loss + AR(1) shadowing + measurement noise, two access
    points, a mobile that mostly walks from A towards B but sometimes turns back). All radio constants are illustrative, not calibrated.
  * FROM THE EMULATOR: the reactive blackout and loss per handoff and the signalling cost of a registration (loss_study / campaign).
  * ANALYTIC: effective blackout = max(0, reactive - lead). The emulator cannot yet start registration before the old link drops;
    adding that (an `anticipation_lead_ms` option in mobility_sim) is the natural validation step and is NOT done here.
Evaluation: trajectories are split into train/validation/test BY TRAJECTORY; the alarm threshold is picked on validation to meet a
false-alarm budget and then frozen; the ML model is compared with a plain RSSI threshold rule tuned to the SAME budget, and with
the reactive baseline.

    PYTHONPATH=backend python3 -m app.compare.ml_handoff --out docs/studies/ml_handoff.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

DT = 0.02                    # 50 Hz RSSI sampling (illustrative)
LINK_LOSS_DBM = -85.0        # serving link counts as lost below this (illustrative)
P0, PL_EXP = -30.0, 3.0      # dBm at 1 m, path-loss exponent (illustrative)
AP_X = (0.0, 120.0)          # access point A and B positions (m)
HORIZON_S = 0.30             # label: link lost within the next 300 ms
EPISODE_GAP_S = 1.0          # alarms closer than this are one alarm episode
GRACE_S = 2.0                # an alarm episode is "true" if the link is lost within this time after it starts
# from the emulator (baseline, median over 24 handoffs; docs/studies/baseline_loss_by_condition.csv + handoffs.csv)
SIM = {"mip-basic": {"blackout_ms": 85.0, "reg_messages": 2, "reg_bytes": 120},
       "ro-model": {"blackout_ms": 160.0, "reg_messages": 6, "reg_bytes": 202}}
RATE_PPS = 100.0
FEATS = ["rssi_a", "rssi_b", "diff", "mean5", "slope10", "slope25", "std10", "min25"]


def trajectory(rng, max_s=60.0):
    n = int(max_s / DT)
    toward = rng.random() < 0.7                         # 30 % never leave: they provide the false-alarm cases
    speed0 = rng.uniform(0.5, 15.0)                     # pedestrian .. vehicle
    x, v = rng.uniform(15.0, 70.0), (speed0 if toward else rng.uniform(-1, 1) * speed0 * 0.3)
    sh = np.zeros((n, 2)); xs = np.empty(n)
    for i in range(n):
        if i:
            v += rng.normal(0, 0.15) + (0.02 * (speed0 - v) if toward else -0.05 * v)
            x += v * DT
            sh[i] = 0.99 * sh[i - 1] + rng.normal(0, 4.0 * np.sqrt(1 - 0.99 ** 2), 2)   # shadowing, sigma 4 dB, ~2 s correlation
        xs[i] = x
    d = np.maximum(np.abs(xs[:, None] - np.array(AP_X)[None, :]), 1.0)
    rssi = P0 - 10 * PL_EXP * np.log10(d) + sh + rng.normal(0, 1.5, (n, 2))             # + measurement noise
    lost = np.where((rssi[:, 0] < LINK_LOSS_DBM))[0]
    end = int(lost[0]) if len(lost) else n
    return rssi[:end + 1], (end if len(lost) else None)


def features(r):
    a, b = r[:, 0], r[:, 1]
    n = len(a)
    def lag(arr, k):
        out = np.empty_like(arr); out[k:] = arr[:-k] if k else arr; out[:k] = arr[0]; return out
    def roll(fn, k):
        pad = np.concatenate([np.full(k - 1, a[0]), a]); w = np.lib.stride_tricks.sliding_window_view(pad, k); return fn(w, axis=1)
    return np.column_stack([a, b, a - b, roll(np.mean, 5), (a - lag(a, 10)) / (10 * DT), (a - lag(a, 25)) / (25 * DT), roll(np.std, 10), roll(np.min, 25)])


def build(n_traj, seed):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n_traj):
        r, ev = trajectory(rng)
        out.append({"X": features(r), "event": ev, "n": len(r)})
    return out


def labels(t):
    y = np.zeros(t["n"], int)
    if t["event"] is not None:
        y[max(0, t["event"] - int(HORIZON_S / DT)):t["event"] + 1] = 1
    return y


def episodes(alarm_idx):
    eps, last = [], None
    for i in alarm_idx:
        if last is None or (i - last) * DT > EPISODE_GAP_S:
            eps.append(i)
        last = i
    return eps


def score(trajs, alarm_fn):
    """Per-trajectory alarms -> (leads for events [s], false alarms, total minutes)."""
    leads, false_alarms, minutes, missed = [], 0, 0.0, 0
    for t in trajs:
        minutes += t["n"] * DT / 60.0
        a = np.where(alarm_fn(t["X"]))[0]
        eps = episodes(a)
        if t["event"] is None:
            false_alarms += len(eps); continue
        ev = t["event"]
        good = [e for e in eps if 0 <= ev - e <= int(GRACE_S / DT)]
        false_alarms += len(eps) - len(good)
        if good:
            leads.append((ev - good[0]) * DT)
        else:
            leads.append(0.0); missed += 1
    return np.array(leads), false_alarms, minutes, missed


def summarise(name, leads, fa, minutes, missed):
    out = {"policy": name, "events": int(len(leads)), "missed_events": int(missed), "false_alarms_per_min": round(fa / minutes, 3),
           "lead_ms_median": round(float(np.median(leads) * 1000), 1), "lead_ms_p10": round(float(np.percentile(leads, 10) * 1000), 1)}
    for ap, c in SIM.items():
        blackout = np.maximum(0.0, c["blackout_ms"] - leads * 1000.0)
        lost = RATE_PPS * blackout / 1000.0
        out[ap] = {"mean_lost_packets_per_handoff": round(float(lost.mean()), 2),
                   "reactive_lost_packets_per_handoff": round(RATE_PPS * c["blackout_ms"] / 1000.0, 2),
                   "share_of_handoffs_with_zero_loss": round(float(np.mean(blackout <= 0)), 3),
                   "extra_signalling_bytes_per_min": round(fa / minutes * c["reg_bytes"], 1)}
    return out


def main(argv=None):
    from sklearn.ensemble import HistGradientBoostingClassifier
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--trajectories", type=int, default=600); ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--fa_budget", type=float, default=1.0, help="false alarms per minute allowed when tuning thresholds")
    a = ap.parse_args(argv)
    data = build(a.trajectories, a.seed)
    k = int(0.6 * len(data)); v = int(0.8 * len(data))
    train, val, test = data[:k], data[k:v], data[v:]
    X = np.vstack([t["X"] for t in train]); y = np.concatenate([labels(t) for t in train])
    keep = (y == 1) | (np.random.default_rng(0).random(len(y)) < 0.25)       # keep all positives, a quarter of negatives
    clf = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08, random_state=0).fit(X[keep], y[keep])
    ml = lambda thr: (lambda Xt: clf.predict_proba(Xt)[:, 1] > thr)
    rule = lambda thr: (lambda Xt: Xt[:, 0] < thr)                           # plain rule: alarm when RSSI_A below thr

    def tune(make, grid, least_sensitive):
        best = None
        for thr in grid:                                                     # most sensitive setting within the false-alarm budget (validation)
            leads, fa, mins, miss = score(val, make(thr))
            if fa / mins <= a.fa_budget and (best is None or np.mean(leads) > best[0]):
                best = (np.mean(leads), thr)
        return best[1] if best else least_sensitive      # nothing met the budget: use the LEAST sensitive setting, never break the budget
    thr_ml = tune(ml, [round(0.30 + 0.02 * i, 2) for i in range(0, 35)], 0.99)          # 0.30 .. 0.98 in 0.02 steps
    thr_rule = tune(rule, [-84.5 + 0.5 * i for i in range(0, 40)], -85.0)               # -84.5 .. -65 dBm in 0.5 dB steps (alarm never fires at -85)
    res = {"setup": {"trajectories": len(data), "train/val/test": [len(train), len(val), len(test)], "test_events": int(sum(t["event"] is not None for t in test)),
                     "test_minutes": round(sum(t["n"] for t in test) * DT / 60, 1), "false_alarm_budget_per_min": a.fa_budget, "ml_threshold": thr_ml,
                     "rule_threshold_dbm": thr_rule, "sample_rate_hz": 1 / DT, "link_loss_dbm": LINK_LOSS_DBM, "horizon_ms": HORIZON_S * 1000},
           "policies": [summarise("reactive (no prediction)", *score(test, lambda Xt: np.zeros(len(Xt), bool))),
                        summarise(f"RSSI threshold rule ({thr_rule} dBm)", *score(test, rule(thr_rule))),
                        summarise(f"ML classifier (p>{thr_ml})", *score(test, ml(thr_ml)))]}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
