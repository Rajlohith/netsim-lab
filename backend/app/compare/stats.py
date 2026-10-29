"""Statistical methods for the Stage 7 experiment analysis (numpy + scipy only; every function is deterministic given its seed).

UNIT OF ANALYSIS. One independent replicate = ONE experiment run (own seed). Several handoffs inside one run share that run's
loss realisation, protocol state and probe grid, so they are NOT independent replicates; the analysis first reduces each run to
one number per metric (see analysis.py) and only then applies the methods below.

METHODS (chosen for small samples of skewed, bounded, partly discrete simulator output; no normality assumption):
  describe()            n, min/quartiles/max, median, mean, sd, and
                          * 95% CI for the MEDIAN: exact distribution-free interval from order statistics (binomial(n, 1/2));
                            its true coverage is >= the nominal level (conservative); reported with the coverage actually achieved,
                            or None when n is too small for the requested level (n < 6 for 95%);
                          * 95% CI for the MEAN: Student t (reported as a convenience; relies on approximate normality of the mean).
  paired_compare()      matched-pairs comparison of two approaches over the same replicate seeds (common random numbers):
                          * effect = median of the paired differences d = a - b, with a seeded percentile-bootstrap CI that
                            resamples PAIRS;
                          * exact/auto Wilcoxon signed-rank test, two-sided (zero differences dropped; all-zero => p = 1);
                          * prob_a_lower = P(d < 0) + P(d = 0)/2 (common-language effect size for pairs).
  holm()                Holm-Bonferroni step-down adjustment (controls family-wise error; no independence assumption).
  required_n()          sample size so that the t-interval half-width for a mean is <= h, given a pilot standard deviation.
A difference is called "distinguishable" only when the Holm-adjusted p < alpha AND the bootstrap CI of the median difference excludes 0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import stats as sps

DEFAULT_CONF = 0.95
DEFAULT_BOOT = 10_000


def _clean(x):
    a = np.asarray([v for v in x if v is not None and not (isinstance(v, float) and math.isnan(v))], dtype=float)
    return a


def median_ci_exact(x, conf: float = DEFAULT_CONF):
    """Exact distribution-free CI for the median: [x_(k), x_(n+1-k)] with k the largest rank such that
    P(Bin(n, .5) <= k-1) <= (1-conf)/2. Returns (lo, hi, achieved_coverage) or None if even k=1 does not reach `conf`."""
    a = np.sort(_clean(x))
    n = len(a)
    if n == 0:
        return None
    alpha = 1.0 - conf
    k = 0
    for j in range(1, n // 2 + 1):
        if sps.binom.cdf(j - 1, n, 0.5) <= alpha / 2:
            k = j
        else:
            break
    if k == 0:
        return None
    coverage = 1.0 - 2.0 * float(sps.binom.cdf(k - 1, n, 0.5))
    return float(a[k - 1]), float(a[n - k]), coverage


def mean_ci_t(x, conf: float = DEFAULT_CONF):
    a = _clean(x)
    n = len(a)
    if n < 2:
        return None
    m, se = float(a.mean()), float(a.std(ddof=1)) / math.sqrt(n)
    if se == 0.0:
        return m, m
    h = float(sps.t.ppf(0.5 + conf / 2, n - 1)) * se
    return m - h, m + h


def bootstrap_ci(x, stat=np.median, conf: float = DEFAULT_CONF, n_boot: int = DEFAULT_BOOT, seed: int = 0):
    """Percentile bootstrap CI of stat(x) (seeded, reproducible)."""
    a = _clean(x)
    if len(a) < 2:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(a), size=(n_boot, len(a)))
    vals = stat(a[idx], axis=1)
    lo, hi = np.percentile(vals, [100 * (1 - conf) / 2, 100 * (1 + conf) / 2])
    return float(lo), float(hi)


def describe(x, conf: float = DEFAULT_CONF) -> dict:
    a = _clean(x)
    n = len(a)
    if n == 0:
        return {"n": 0}
    q1, med, q3 = (float(v) for v in np.percentile(a, [25, 50, 75]))
    d = {"n": n, "min": float(a.min()), "q1": q1, "median": med, "q3": q3, "max": float(a.max()), "mean": float(a.mean()),
         "sd": float(a.std(ddof=1)) if n > 1 else None, "degenerate": bool(np.all(a == a[0])),
         "median_ci_lo": None, "median_ci_hi": None, "median_ci_coverage": None, "mean_ci_lo": None, "mean_ci_hi": None,
         "ci_note": ""}
    mc = median_ci_exact(a, conf)
    if mc:
        d["median_ci_lo"], d["median_ci_hi"], d["median_ci_coverage"] = mc
    else:
        d["ci_note"] = f"n={n} is too small for an exact {conf:.0%} CI of the median (needs n >= 6)"
    mt = mean_ci_t(a, conf)
    if mt:
        d["mean_ci_lo"], d["mean_ci_hi"] = mt
    if d["degenerate"] and n > 1:
        d["ci_note"] = (d["ci_note"] + "; " if d["ci_note"] else "") + \
            "all replicates identical: the interval has zero width because the simulator output did not vary, not because the value is known exactly"
    return d


def holm(pvalues) -> list:
    """Holm-Bonferroni adjusted p-values (same order as the input; None stays None)."""
    idx = [i for i, p in enumerate(pvalues) if p is not None]
    order = sorted(idx, key=lambda i: pvalues[i])
    m, out, running = len(order), [None] * len(pvalues), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        out[i] = running
    return out


@dataclass
class PairedResult:
    n_pairs: int
    median_diff: float | None
    diff_ci_lo: float | None
    diff_ci_hi: float | None
    p_value: float | None
    prob_a_lower: float | None
    note: str = ""

    def to_dict(self):
        return dict(self.__dict__)


def paired_compare(a, b, conf: float = DEFAULT_CONF, n_boot: int = DEFAULT_BOOT, seed: int = 0) -> PairedResult:
    """a, b: equally long sequences, element i of each belongs to the same replicate seed. Pairs with a missing value in either
    sequence are dropped (the count of pairs actually used is returned). d = a - b, so a NEGATIVE effect means a is lower."""
    if len(a) != len(b):
        raise ValueError("paired_compare needs sequences of equal length")
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None
             and not (isinstance(x, float) and math.isnan(x)) and not (isinstance(y, float) and math.isnan(y))]
    n = len(pairs)
    if n == 0:
        return PairedResult(0, None, None, None, None, None, "no complete pairs")
    d = np.array([x - y for x, y in pairs], dtype=float)
    med = float(np.median(d))
    ci = bootstrap_ci(d, np.median, conf, n_boot, seed) if n >= 2 else None
    p = None
    note = ""
    nz = d[d != 0]
    if len(nz) == 0:
        p, note = 1.0, "all paired differences are zero"
    elif n < 6:
        note = f"n={n} pairs: too few for a Wilcoxon test to reach p < 0.05"
        p = float(sps.wilcoxon(nz, method="auto").pvalue)
    else:
        p = float(sps.wilcoxon(nz, method="auto", zero_method="wilcox").pvalue)
    pa = float(((d < 0).sum() + 0.5 * (d == 0).sum()) / n)
    return PairedResult(n, med, None if ci is None else ci[0], None if ci is None else ci[1], p, pa, note)


def verdict(res: PairedResult, p_adj: float | None, alpha: float = 0.05) -> str:
    """'a lower' / 'b lower' / 'no distinguishable difference' / 'insufficient data'. Never 'better': direction only."""
    if res.n_pairs < 6 or p_adj is None or res.diff_ci_lo is None:
        return "insufficient data"
    if p_adj < alpha and (res.diff_ci_hi < 0):
        return "a lower"
    if p_adj < alpha and (res.diff_ci_lo > 0):
        return "b lower"
    return "no distinguishable difference"


def required_n(sd: float, halfwidth: float, conf: float = DEFAULT_CONF, n_max: int = 100_000) -> int:
    """Smallest n with t_{n-1,(1+conf)/2} * sd / sqrt(n) <= halfwidth (derived estimate from a PILOT sd; not a guarantee)."""
    if sd < 0 or halfwidth <= 0:
        raise ValueError("sd must be >= 0 and halfwidth > 0")
    if sd == 0:
        return 2
    for n in range(2, n_max):
        if float(sps.t.ppf(0.5 + conf / 2, n - 1)) * sd / math.sqrt(n) <= halfwidth:
            return n
    raise ValueError("required n exceeds n_max")


def cluster_bootstrap_ratio(num, den, conf: float = DEFAULT_CONF, n_boot: int = DEFAULT_BOOT, seed: int = 0):
    """CI of sum(num)/sum(den) resampling CLUSTERS (runs) with replacement: items inside one run are not independent, so the run
    is the resampling unit. num/den are per-run counts. Returns (lo, hi) or None."""
    num, den = np.asarray(num, dtype=float), np.asarray(den, dtype=float)
    n = len(num)
    if n < 2 or den.sum() <= 0:
        return None
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    d = den[idx].sum(axis=1)
    ok = d > 0
    if not ok.any():
        return None
    r = num[idx].sum(axis=1)[ok] / d[ok]
    lo, hi = np.percentile(r, [100 * (1 - conf) / 2, 100 * (1 + conf) / 2])
    return float(lo), float(hi)
