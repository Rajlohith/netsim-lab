import unittest

import numpy as np
import pandas as pd

from app.ml.config import MinData
from app.ml.splits import (InsufficientDataError, LeakageError, SPLITS, assert_no_leakage, group_split, split_frames)


def make_df(n_scen=9, runs=3, pos_every=4):
    rows = []
    for s in range(n_scen):
        for r in range(runs):
            for k in range(40):
                lab = int(k % pos_every == 0)
                rows.append({"scenario_id": f"S{s}", "experiment_id": f"S{s}/r{r}", "t_ns": k, "label": lab, "status": "included",
                             "episode_event_id": f"h{k // 8}" if lab else ""})
    return pd.DataFrame(rows)


class SplitTests(unittest.TestCase):
    def test_scenario_split_is_group_disjoint(self):
        df = make_df()
        sp = group_split(df, by="scenario", seed=1)
        fr = split_frames(df, sp)
        for key in ("scenario_id", "experiment_id"):
            sets = [set(fr[s][key]) for s in SPLITS]
            self.assertFalse(sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])
        self.assertEqual(sum(len(f) for f in fr.values()), len(df))           # every row lands somewhere exactly once

    def test_experiment_split_keeps_each_run_whole(self):
        df = make_df(n_scen=3, runs=6)
        sp = group_split(df, by="experiment", seed=0)
        fr = split_frames(df, sp)
        total = {e for f in fr.values() for e in f["experiment_id"]}
        self.assertEqual(len(total), 18)
        assert_no_leakage(fr, "experiment")

    def test_split_is_deterministic(self):
        df = make_df()
        self.assertEqual(group_split(df, seed=3).assignment, group_split(df, seed=3).assignment)

    def test_leakage_detector_fires_on_shared_group(self):
        df = make_df()
        fr = split_frames(df, group_split(df))
        fr["test"] = pd.concat([fr["test"], fr["train"].head(5)])
        with self.assertRaises(LeakageError):
            assert_no_leakage(fr, "scenario")

    def test_random_row_split_would_be_caught(self):
        df = make_df()
        idx = np.random.default_rng(0).permutation(len(df))
        n = len(df)
        fr = {"train": df.iloc[idx[: int(.6 * n)]], "validation": df.iloc[idx[int(.6 * n): int(.8 * n)]], "test": df.iloc[idx[int(.8 * n):]]}
        with self.assertRaises(LeakageError):
            assert_no_leakage(fr, "experiment")

    def test_too_few_groups_is_insufficient(self):
        with self.assertRaises(InsufficientDataError) as c:
            group_split(make_df(n_scen=3), by="scenario")
        self.assertIn("need >= 6", str(c.exception))

    def test_too_few_positives_is_insufficient(self):
        df = make_df()
        df["label"] = 0
        df.loc[df.index[:3], "label"] = 1
        with self.assertRaises(InsufficientDataError):
            group_split(df, min_data=MinData())


if __name__ == "__main__":
    unittest.main()
