"""Uncertainty. Every reported number carries an interval.

NO POINT ESTIMATE IS EVER REPORTED ALONE. A mean forward return of +2.1
points is meaningless without knowing whether the interval is
[+1.9, +2.3] or [-8, +12]. The second is the common case and it is
indistinguishable from the first in a table of means.

Implemented in pure Python -- no numpy, no scipy. The arithmetic is
simple, and the alternative is adding two large dependencies to a
service whose entire runtime is currently one driver. The bootstrap is
SEEDED, so a result is reproducible exactly.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass
from typing import List, Optional, Sequence

# Below this, an interval is so wide that any conclusion is an artefact
# of the sample rather than a property of the market.
MIN_SAMPLES_FOR_ANY_CLAIM = 30
# Below this, a subgroup comparison is not attempted at all.
MIN_SAMPLES_FOR_SUBGROUP = 50


class InferenceError(Exception):
    pass


@dataclass(frozen=True)
class Estimate:
    """A statistic with its uncertainty and the sample it came from."""
    n: int
    mean: float
    median: float
    stdev: float
    ci_low: float
    ci_high: float
    confidence: float
    seed: int

    @property
    def excludes_zero(self) -> bool:
        return (self.ci_low > 0) or (self.ci_high < 0)

    @property
    def adequately_powered(self) -> bool:
        return self.n >= MIN_SAMPLES_FOR_ANY_CLAIM

    def as_row(self) -> dict:
        return {"n": self.n, "mean": round(self.mean, 6),
                "median": round(self.median, 6), "stdev": round(self.stdev, 6),
                "ci_low": round(self.ci_low, 6), "ci_high": round(self.ci_high, 6),
                "confidence": self.confidence, "excludes_zero": self.excludes_zero,
                "seed": self.seed}


def bootstrap_mean(values: Sequence[float], confidence: float = 0.95,
                   iterations: int = 2000, seed: int = 20260907) -> Estimate:
    """Percentile bootstrap.

    Chosen over a t-interval because forward returns are visibly
    non-normal -- fat tailed and often skewed -- and a t-interval on such
    data is narrower than the truth, which is the direction of error that
    manufactures false findings.
    """
    vals = [float(v) for v in values]
    if len(vals) < 2:
        raise InferenceError("need at least two observations")
    rng = random.Random(seed)
    n = len(vals)
    means: List[float] = []
    for _ in range(iterations):
        means.append(sum(vals[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    alpha = (1.0 - confidence) / 2.0
    lo = means[int(alpha * iterations)]
    hi = means[min(iterations - 1, int((1 - alpha) * iterations))]
    return Estimate(
        n=n, mean=statistics.fmean(vals), median=statistics.median(vals),
        stdev=statistics.pstdev(vals) if n > 1 else 0.0,
        ci_low=lo, ci_high=hi, confidence=confidence, seed=seed,
    )


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def two_sided_p(values: Sequence[float]) -> float:
    """p-value for "the mean is zero", normal approximation.

    Reported alongside the interval, never instead of it. A p-value
    answers a narrower question than most readers assume, and on its own
    it invites exactly the threshold-chasing this ticket forbids.
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 2:
        raise InferenceError("need at least two observations")
    sd = statistics.pstdev(vals)
    if sd == 0:
        return 0.0 if statistics.fmean(vals) != 0 else 1.0
    z = statistics.fmean(vals) / (sd / math.sqrt(n))
    return 2.0 * (1.0 - normal_cdf(abs(z)))


def _inverse_normal_cdf(p: float) -> float:
    """Acklam's rational approximation. Accurate to ~1e-9, which is far
    beyond what a sample-size rule needs."""
    if not 0 < p < 1:
        raise InferenceError("p must be in (0,1)")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def minimum_n(effect_size: float, power: float = 0.80,
              alpha: float = 0.05) -> int:
    """Samples needed to detect a standardised effect, two-sided.

    Used to decide INCONCLUSIVE honestly. Without it, "no significant
    effect" on 40 observations gets written up as evidence of no edge,
    when the study could never have detected one.
    """
    if effect_size <= 0:
        raise InferenceError("effect size must be positive")
    z_a = _inverse_normal_cdf(1 - alpha / 2)
    z_b = _inverse_normal_cdf(power)
    return math.ceil(((z_a + z_b) / effect_size) ** 2)
