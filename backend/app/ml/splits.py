"""Leakage-safe splitting. Rows of one run are strongly autocorrelated (overlapping windows, shared schedule), so splitting
by row would put near-duplicates in train and test. We split by GROUP:

  by='scenario'   all runs of a scenario family (same approach + parameters; only the movement schedule differs) go to one split
  by='experiment' all rows of one run go to one split (weaker: sibling runs of a family may be on both sides)

`assert_no_leakage` re-checks the finished split and is called by the pipeline and the tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import MinData

KEY = {"scenario": "scenario_id", "experiment": "experiment_id"}
SPLITS = ("train", "validation", "test")


class InsufficientDataError(Exception):
    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


class LeakageError(AssertionError):
    pass


@dataclass
class SplitResult:
    by: str
    seed: int
    fractions: tuple
    attempt: int
    assignment: dict = field(default_factory=dict)      # group -> split name

    def to_dict(self):
        out = {s: sorted(g for g, v in self.assignment.items() if v == s) for s in SPLITS}
        return {"by": self.by, "seed": self.seed, "fractions": list(self.fractions), "seed_attempt_used": self.attempt, "groups": out}


def _sizes(n, fractions):
    n_val = max(1, int(round(n * fractions[1])))
    n_test = max(1, int(round(n * fractions[2])))
    n_train = n - n_val - n_test
    return n_train, n_val, n_test


def split_frames(df: pd.DataFrame, split: SplitResult) -> dict:
    key = KEY[split.by]
    return {s: df[df[key].map(split.assignment) == s].copy() for s in SPLITS}


def sufficiency_problems(frames: dict, min_data: MinData, stratify: str | None = None) -> list:
    problems = []
    if stratify:
        for s in SPLITS:
            inc = frames[s][frames[s]["status"] == "included"]
            for st in sorted(set(frames["train"][stratify]) | set(inc[stratify])):
                pos = int(((inc[stratify] == st) & (inc["label"] == 1)).sum())
                if pos < min_data.min_positive_examples_per_stratum:
                    problems.append(f"{s}: stratum {stratify}={st!r} has {pos} positive examples < {min_data.min_positive_examples_per_stratum}")
    for s in SPLITS:
        f = frames[s]
        inc = f[f["status"] == "included"]
        pos = int((inc["label"] == 1).sum())
        eps = inc.loc[inc["label"] == 1, ["experiment_id", "episode_event_id"]].drop_duplicates().shape[0]
        if f["scenario_id"].nunique() < 1 or f["experiment_id"].nunique() < 1:
            problems.append(f"{s}: no groups")
        if pos < min_data.min_positive_examples_per_split:
            problems.append(f"{s}: {pos} positive examples < {min_data.min_positive_examples_per_split}")
        if eps < min_data.min_episodes_per_split:
            problems.append(f"{s}: {eps} degradation episodes < {min_data.min_episodes_per_split}")
    tr = frames["train"]
    neg = int(((tr["status"] == "included") & (tr["label"] == 0)).sum())
    if neg < min_data.min_train_negatives:
        problems.append(f"train: {neg} negative examples < {min_data.min_train_negatives}")
    return problems


def group_split(df: pd.DataFrame, *, by: str = "scenario", fractions=(0.6, 0.2, 0.2), seed: int = 0,
                min_data: MinData | None = None, max_attempts: int = 200, stratify: str | None = None) -> SplitResult:
    """Deterministic grouped split. The seed is advanced (seed, seed+1, ...) until every split has enough POSITIVE EXAMPLES AND
    EPISODES (a property of the labels, chosen before any model is trained - no model score is involved)."""
    min_data = min_data or MinData()
    if by not in KEY:
        raise ValueError(f"by must be one of {sorted(KEY)}")
    key = KEY[by]
    groups = sorted(df.loc[df["status"] == "included", key].unique().tolist())
    need = 3 * min_data.min_groups_per_split
    if len(groups) < need:
        raise InsufficientDataError([f"{len(groups)} {by} group(s) with usable examples, need >= {need} "
                                     f"({min_data.min_groups_per_split} per split)"])
    # optional stratification: groups are split separately inside each stratum (e.g. each dwell model), so every split sees every stratum
    strata = {}
    for g in groups:
        key_vals = df.loc[(df[key] == g) & (df["status"] == "included"), stratify].unique() if stratify else [""]
        strata.setdefault(str(key_vals[0]) if len(key_vals) else "", []).append(g)
    best = None
    for attempt in range(max_attempts):
        rng = np.random.default_rng(seed + attempt)
        assign = {}
        for _st, gs in sorted(strata.items()):
            n_tr, n_va, n_te = _sizes(len(gs), fractions)
            if min(n_tr, n_va, n_te) < 1:
                raise InsufficientDataError([f"stratum {_st!r} has only {len(gs)} group(s): cannot place it in all three splits"])
            perm = [gs[i] for i in rng.permutation(len(gs))]
            assign.update({g: ("train" if i < n_tr else "validation" if i < n_tr + n_va else "test") for i, g in enumerate(perm)})
        res = SplitResult(by, seed, tuple(fractions), attempt, assign)
        problems = sufficiency_problems(split_frames(df, res), min_data, stratify)
        if not problems:
            assert_no_leakage(split_frames(df, res), by)
            return res
        best = best or problems
    raise InsufficientDataError(best or ["no valid split found"])


def assert_no_leakage(frames: dict, by: str):
    """Raises LeakageError unless train/validation/test are pairwise disjoint in experiment_id AND in the grouping key."""
    keys = {"experiment_id"} | {KEY[by]}
    for k in keys:
        sets = {s: set(frames[s][k].unique()) for s in SPLITS}
        for a in range(3):
            for b in range(a + 1, 3):
                both = sets[SPLITS[a]] & sets[SPLITS[b]]
                if both:
                    raise LeakageError(f"{k} shared between {SPLITS[a]} and {SPLITS[b]}: {sorted(both)[:5]}")
