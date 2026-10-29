"""Stage 7: statistical calculations (app.compare.stats). Known values are derived by hand or from the binomial distribution."""
import math
import unittest

import numpy as np
from scipy import stats as sps

from app.compare import stats as st


class MedianCI(unittest.TestCase):
    def test_exact_interval_and_coverage_n10(self):
        x = list(range(1, 11))                                   # 1..10
        lo, hi, cov = st.median_ci_exact(x, 0.95)
        # k=2: P(Bin(10,.5) <= 1) = 11/1024 <= 0.025 ; k=3: P(<=2)=56/1024 > 0.025
        self.assertEqual((lo, hi), (2.0, 9.0))
        self.assertAlmostEqual(cov, 1 - 2 * 11 / 1024, places=12)
        self.assertGreaterEqual(cov, 0.95)

    def test_n6_is_the_smallest_sample_for_95(self):
        lo, hi, cov = st.median_ci_exact([5, 1, 4, 2, 6, 3], 0.95)
        self.assertEqual((lo, hi), (1.0, 6.0))
        self.assertAlmostEqual(cov, 1 - 2 / 64)
        self.assertIsNone(st.median_ci_exact([1, 2, 3, 4, 5], 0.95))      # n=5: best possible coverage is 93.75%
        self.assertIsNone(st.median_ci_exact([], 0.95))

    def test_empirical_coverage_is_at_least_nominal(self):
        rng = np.random.default_rng(1)
        hit = 0
        trials = 3000
        for _ in range(trials):
            lo, hi, _c = st.median_ci_exact(rng.standard_normal(15), 0.95)
            hit += lo <= 0.0 <= hi                                          # true median of N(0,1) is 0
        self.assertGreaterEqual(hit / trials, 0.94)                         # conservative by construction; 0.94 leaves Monte-Carlo slack

    def test_none_and_nan_are_ignored(self):
        self.assertEqual(st.describe([1, None, 3, float("nan"), 2])["n"], 3)


class Describe(unittest.TestCase):
    def test_values(self):
        d = st.describe([2, 4, 4, 4, 5, 5, 7, 9])
        self.assertEqual((d["n"], d["min"], d["max"], d["median"]), (8, 2.0, 9.0, 4.5))
        self.assertAlmostEqual(d["mean"], 5.0)
        self.assertAlmostEqual(d["sd"], math.sqrt(32 / 7))
        lo, hi = d["mean_ci_lo"], d["mean_ci_hi"]
        h = sps.t.ppf(0.975, 7) * d["sd"] / math.sqrt(8)
        self.assertAlmostEqual(lo, 5 - h)
        self.assertAlmostEqual(hi, 5 + h)

    def test_degenerate_sample_is_flagged_not_trusted(self):
        d = st.describe([3.0] * 8)
        self.assertTrue(d["degenerate"])
        self.assertEqual((d["median_ci_lo"], d["median_ci_hi"]), (3.0, 3.0))
        self.assertIn("did not vary", d["ci_note"])

    def test_small_n_has_no_median_ci_and_says_why(self):
        d = st.describe([1.0, 2.0, 4.0])
        self.assertIsNone(d["median_ci_lo"])
        self.assertIn("too small", d["ci_note"])

    def test_empty(self):
        self.assertEqual(st.describe([]), {"n": 0})


class Bootstrap(unittest.TestCase):
    def test_seeded_and_reproducible(self):
        x = [1.3, 2.1, 3.7, 4.2, 10.9, 11.4, 12.8, 30.5]
        a = st.bootstrap_ci(x, np.mean, 0.95, 2000, seed=5)
        b = st.bootstrap_ci(x, np.mean, 0.95, 2000, seed=5)
        c = st.bootstrap_ci(x, np.mean, 0.95, 2000, seed=6)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertLessEqual(a[0], np.mean(x))
        self.assertGreaterEqual(a[1], np.mean(x))

    def test_cluster_bootstrap_ratio_resamples_runs(self):
        num, den = [1, 0, 2, 1], [2, 1, 2, 2]
        lo, hi = st.cluster_bootstrap_ratio(num, den, 0.95, 2000, seed=1)
        self.assertLessEqual(lo, 4 / 7)
        self.assertGreaterEqual(hi, 4 / 7)
        self.assertIsNone(st.cluster_bootstrap_ratio([1], [1]))
        self.assertIsNone(st.cluster_bootstrap_ratio([0, 0], [0, 0]))


class Multiplicity(unittest.TestCase):
    def test_holm_known_example(self):
        self.assertEqual([round(p, 10) for p in st.holm([0.01, 0.04, 0.03])], [0.03, 0.06, 0.06])

    def test_holm_is_monotone_capped_and_keeps_none(self):
        out = st.holm([0.5, None, 0.001, 0.9])
        self.assertIsNone(out[1])
        self.assertLessEqual(max(p for p in out if p is not None), 1.0)
        self.assertGreaterEqual(out[0], 0.5)
        self.assertEqual(out[2], 0.003)


class Paired(unittest.TestCase):
    def test_clear_difference(self):
        a = [10 + i * 0.1 for i in range(12)]
        b = [x + 5 + (i % 3) * 0.2 for i, x in enumerate(a)]               # b always larger
        r = st.paired_compare(a, b, 0.95, 2000, seed=3)
        self.assertEqual(r.n_pairs, 12)
        self.assertLess(r.median_diff, 0)
        self.assertLess(r.diff_ci_hi, 0)
        self.assertLess(r.p_value, 0.001)                                   # exact two-sided p = 2/2^12
        self.assertAlmostEqual(r.p_value, 2 / 2 ** 12, places=9)
        self.assertEqual(r.prob_a_lower, 1.0)
        self.assertEqual(st.verdict(r, r.p_value), "a lower")

    def test_direction_flips(self):
        a = [20.0 + i for i in range(10)]
        b = [x - 3 - (i % 2) * 0.5 for i, x in enumerate(a)]
        r = st.paired_compare(a, b, 0.95, 2000, seed=3)
        self.assertEqual(st.verdict(r, r.p_value), "b lower")

    def test_identical_series_have_p_one_and_no_difference(self):
        a = [1.0] * 10
        r = st.paired_compare(a, a, 0.95, 500, seed=0)
        self.assertEqual(r.p_value, 1.0)
        self.assertEqual(r.median_diff, 0.0)
        self.assertEqual(st.verdict(r, 1.0), "no distinguishable difference")

    def test_missing_values_drop_pairs(self):
        a = [1, 2, None, 4, 5, 6, 7, 8]
        b = [2, 3, 4, None, 6, 7, 8, 9]
        self.assertEqual(st.paired_compare(a, b, 0.95, 200, seed=0).n_pairs, 6)
        self.assertEqual(st.paired_compare([None], [1], 0.95, 200).n_pairs, 0)
        with self.assertRaises(ValueError):
            st.paired_compare([1, 2], [1])

    def test_too_few_pairs_is_insufficient_not_significant(self):
        r = st.paired_compare([1, 1, 1], [5, 5, 5], 0.95, 500, seed=0)
        self.assertEqual(st.verdict(r, 0.25), "insufficient data")

    def test_overlapping_noise_is_not_distinguishable(self):
        rng = np.random.default_rng(9)
        a = rng.normal(10, 1, 20)
        b = a + rng.normal(0, 1, 20)
        r = st.paired_compare(list(a), list(b), 0.95, 2000, seed=3)
        self.assertEqual(st.verdict(r, r.p_value), "no distinguishable difference")

    def test_type_one_error_rate_under_the_null(self):
        rng = np.random.default_rng(11)
        rej = 0
        trials = 400
        for _ in range(trials):
            a = rng.normal(0, 1, 15)
            b = a + rng.normal(0, 1, 15)
            rej += st.paired_compare(list(a), list(b), 0.95, 200, seed=0).p_value < 0.05
        self.assertLess(rej / trials, 0.09)                                 # nominal 5%, generous Monte-Carlo slack


class SampleSize(unittest.TestCase):
    def test_required_n_is_minimal(self):
        n = st.required_n(sd=10.0, halfwidth=2.0, conf=0.95)
        t = lambda k: sps.t.ppf(0.975, k - 1) * 10 / math.sqrt(k)
        self.assertLessEqual(t(n), 2.0)
        self.assertGreater(t(n - 1), 2.0)
        self.assertEqual(st.required_n(0.0, 1.0), 2)
        with self.assertRaises(ValueError):
            st.required_n(1.0, 0.0)


if __name__ == "__main__":
    unittest.main()
