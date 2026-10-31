"""Theil-Sen slope with the (modified) Mann-Kendall test (PLAN §7.1 step 5). Pure.

**Mann-Kendall** (Mann 1945; Kendall 1975) tests for a monotone trend without assuming a
distribution. With the points in time order,

    S = Σ_{i<j} sign(y_j - y_i)

and, for ``n`` points whose tied groups have sizes ``u_p``,

    Var(S) = [ n(n-1)(2n+5) - Σ_p u_p(u_p-1)(2u_p+5) ] / 18         (Kendall 1975)
    Z = (S - 1)/√Var   if S > 0,   0 if S = 0,   (S + 1)/√Var   if S < 0
    p = 2 · (1 - Φ(|Z|))

The test uses only the order of the values, so it does not depend on how the points are spaced
in time. Unevenly spaced data are fine; **tied times are not** (the variance above assumes
none): aggregate to one value per day first, as ``daily_values`` does. A constant series has
``Var(S) = 0``: Z = 0, p = 1.

**Hamed-Rao** (Hamed and Rao 1998). Positive autocorrelation makes the plain test see trends in
noise, because the points carry less independent information than ``n``. Hamed and Rao remove
the Theil-Sen trend, rank the residuals, take the autocorrelation rho_k of the ranks for the lags
where it is significant at the 95 % level (Anderson's limits
``(-1 ± 1.96 √(n-k-1)) / (n-k)``), and inflate the variance by

    n/n* = 1 + 2 / (n(n-1)(n-2)) · Σ_{k=1}^{n-1} (n-k)(n-k-1)(n-k-2) · rho_k

so that ``Var*(S) = Var(S) · n/n*``. Our decisions where the paper leaves room:

* the residuals use the real times (``y - slope · (t - t_0)``); the lag ``k`` of the
  autocorrelation counts points in time order, so on a series with gaps a lag is "k readings
  later", not "k days later". It is an approximation of the autocorrelation in time, and the
  correction is only as good as that;
* **``n/n*`` is never below 1**: only inflation is applied. The paper's factor drops below 1 for
  negative autocorrelation, which would make the test claim more than the plain one; we take the
  conservative side. The unclamped value is reported as ``n_over_n_star_raw``.

**AR(1) correction** (the default; ``Correction.AR1``). Hamed-Rao uses only the lags whose sample
autocorrelation is significant, so it under-corrects (docs/dev/analysis-methods.md has the measured
rates: 9.5 % at phi 0.3 and 15.4 % at phi 0.6 for a test at 5 %). The AR(1) correction keeps the
same variance formula and weights but takes the autocorrelation from a fitted AR(1) model at
every lag, ``rho_k = phi^k`` (the effective sample size of an AR(1) series, Bayley and Hammersley
1946; used for Mann-Kendall by Yue and Wang 2004), so ``n/n*`` is a smooth function of one
estimated number, ``phi``:

    r     = 2 sin(pi r_1 / 6)            r_1: lag-1 autocorrelation of the ranks of the residuals;
                                         the map takes a rank correlation back to the Gaussian
                                         scale (Pearson 1907; Kruskal 1958), so outliers do not
                                         move it and AR(1) data give r near phi
    phi   = clip(r + (6 + 6 r)/n, 0, 0.95)

``(6 + 6 r)/n`` is two things: the downward bias of a lag-1 autocorrelation estimated after a mean
and a trend are removed, about ``(2 + 5.5 phi)/n`` (Kendall 1954; Marriott and Pope 1954 for the
mean alone; measured here for the detrended case), and a safety margin of about ``4/n`` for the
spread of ``r`` in short series, where one does not know ``phi`` well and the cost of guessing
low is a false trend. The constants are calibrated by simulation, not taken from a paper (see
``docs/dev/analysis-methods.md``); ``phi`` is never below 0 (the test is only ever made more
cautious) and never above 0.95. ``S`` and its tie-corrected variance are those of the data;
only ``Var(S)`` is multiplied, by ``n/n*``, so the slope, its interval (which widens with the
variance) and the direction are the Theil-Sen ones.

**Theil-Sen** (Theil 1950; Sen 1968) is the median of the slopes ``(y_j - y_i)/(t_j - t_i)`` over
all pairs; it uses the real times, so the slope is in value units per day, and is robust to
outliers (breakdown point about 29 %). Its confidence interval is Sen's (1968), as in Gilbert
(1987) §17.3 and Helsel and Hirsch (2002): with ``N`` slopes sorted ascending and
``C = z_(1-alpha/2) · √Var*(S)``, the limits are the slopes of ranks ``(N - C)/2`` and
``(N + C)/2 + 1`` (1-based, linearly interpolated, clipped to ``[1, N]``). The variance is the
modified one, so the interval widens with autocorrelation as the test does. The intercept is the
median of ``y - slope · (t - t_0)``, the level at ``t_0``, the first time.

The ``n(n-1)/2`` pairwise slopes are held in memory, so at most ``MAX_POINTS`` points are
accepted; trends are computed over windows of weeks to a year, far below it.
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from statistics import NormalDist
from typing import Any

import numpy as np
from scipy.stats import rankdata

from ownvitals.analysis.methods.series import FloatArray, InputSummary, Series

ALGO_VERSION = "trend/2"

MIN_POINTS = 3
MAX_POINTS = 3000
DEFAULT_ALPHA = 0.05
ACF_ALPHA = 0.05  # the level at which Hamed and Rao call an autocorrelation significant
AR1_BIAS_CONSTANT = 6.0  # phi = r + (AR1_BIAS_CONSTANT + AR1_BIAS_SLOPE * r) / n, see the docstring
AR1_BIAS_SLOPE = 6.0
AR1_MAX = 0.95  # the largest phi used: 1 would be a random walk, with no finite variance


class Correction(StrEnum):
    NONE = "none"  # plain Mann-Kendall
    HAMED_RAO = "hamed_rao"  # Hamed and Rao 1998: the significant lags only (under-corrects)
    AR1 = "ar1"  # the AR(1)-fitted effective sample size (the default)


class Direction(StrEnum):
    INCREASING = "increasing"
    DECREASING = "decreasing"
    NONE = "none"  # no significant trend


@dataclass(frozen=True, slots=True)
class TrendResult:
    """The trend of a series; ``slope`` and its interval are per day (units of ``y`` per day)."""

    s: int
    tau: float  # S / (n(n-1)/2)
    var_s: float  # plain, tie-corrected
    n_over_n_star_raw: float  # Hamed-Rao factor as published (1.0 without the correction)
    n_over_n_star: float  # the factor used: never below 1
    var_s_used: float  # var_s * n_over_n_star
    significant_lags: tuple[int, ...]
    ar1_r: float | None  # AR1: lag-1 autocorrelation of the residual ranks, on the Gaussian scale
    ar1_phi: float | None  # AR1: the coefficient used for n/n* (after bias, margin and limits)
    z: float
    p_value: float
    alpha: float
    significant: bool
    direction: Direction
    slope: float
    slope_low: float
    slope_high: float
    intercept: float  # level at t_ref
    t_ref: float
    correction: Correction
    inputs: InputSummary
    algo_version: str = ALGO_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "s": self.s,
            "tau": self.tau,
            "var_s": self.var_s,
            "n_over_n_star_raw": self.n_over_n_star_raw,
            "n_over_n_star": self.n_over_n_star,
            "var_s_used": self.var_s_used,
            "significant_lags": list(self.significant_lags),
            "ar1_r": self.ar1_r,
            "ar1_phi": self.ar1_phi,
            "z": self.z,
            "p_value": self.p_value,
            "alpha": self.alpha,
            "significant": self.significant,
            "direction": self.direction.value,
            "slope_per_day": self.slope,
            "slope_low": self.slope_low,
            "slope_high": self.slope_high,
            "intercept": self.intercept,
            "t_ref": self.t_ref,
            "correction": self.correction.value,
            "inputs": self.inputs.as_dict(),
            "algo_version": self.algo_version,
        }


def mann_kendall_variance(y: FloatArray) -> float:
    """``Var(S)`` with the tie correction, for the values ``y`` (Kendall 1975)."""
    n = int(y.size)
    _, counts = np.unique(y, return_counts=True)
    u = counts.astype(np.float64)
    ties = float(np.sum(u * (u - 1.0) * (2.0 * u + 5.0)))
    return (n * (n - 1.0) * (2.0 * n + 5.0) - ties) / 18.0


def mann_kendall_z(s: int, var_s: float) -> float:
    """The continuity-corrected Z of ``S``; 0 when ``S`` is 0 or the variance is."""
    if s == 0 or var_s <= 0.0:
        return 0.0
    root = math.sqrt(var_s)
    return (s - 1) / root if s > 0 else (s + 1) / root


def two_sided_p(z: float) -> float:
    """``2 · (1 - Φ(|z|))``."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def pairwise(t: FloatArray, y: FloatArray) -> tuple[int, FloatArray]:
    """``S`` and the ascending pairwise slopes of points with strictly increasing times."""
    n = int(y.size)
    slopes = np.empty(n * (n - 1) // 2)
    s = 0
    at = 0
    for i in range(n - 1):
        dy = y[i + 1 :] - y[i]
        s += int(np.sign(dy).sum())
        count = dy.size
        slopes[at : at + count] = dy / (t[i + 1 :] - t[i])
        at += count
    slopes.sort()
    return s, slopes


def autocorrelation(values: FloatArray) -> FloatArray:
    """The sample autocorrelation at lags 0 to n-1 (denominator: the sum of squares); zeros for a
    constant input."""
    n = int(values.size)
    centred = values - values.mean()
    denominator = float(centred @ centred)
    if denominator == 0.0:
        return np.zeros(n)
    return np.correlate(centred, centred, mode="full")[n - 1 :] / denominator


def hamed_rao_factor(
    ranks: FloatArray, *, alpha: float = ACF_ALPHA
) -> tuple[float, tuple[int, ...]]:
    """``n/n*`` of Hamed and Rao (1998) from the ranks of the detrended series, and the lags
    whose autocorrelation was significant. Not clamped."""
    n = int(ranks.size)
    if n < MIN_POINTS:
        raise ValueError(f"the Hamed-Rao factor needs at least {MIN_POINTS} points")
    rho = autocorrelation(ranks)
    z = NormalDist().inv_cdf(1.0 - alpha / 2.0)
    total = 0.0
    lags: list[int] = []
    for k in range(1, n - 2):  # (n-k)(n-k-1)(n-k-2) is zero from k = n-2 on
        lower = (-1.0 - z * math.sqrt(n - k - 1)) / (n - k)
        upper = (-1.0 + z * math.sqrt(n - k - 1)) / (n - k)
        if rho[k] < lower or rho[k] > upper:
            lags.append(k)
            total += (n - k) * (n - k - 1) * (n - k - 2) * float(rho[k])
    return 1.0 + 2.0 * total / (n * (n - 1.0) * (n - 2.0)), tuple(lags)


def ar1_coefficient(ranks: FloatArray) -> tuple[float, float]:
    """``(r, phi)`` for the ranks of the detrended series: ``r`` is the lag-1 autocorrelation of
    the ranks mapped to the Gaussian scale, ``phi`` the AR(1) coefficient the correction uses
    (see the module docstring). A constant input has ``r = 0``."""
    n = int(ranks.size)
    if n < MIN_POINTS:
        raise ValueError(f"the AR(1) coefficient needs at least {MIN_POINTS} points")
    centred = ranks - ranks.mean()
    denominator = float(centred @ centred)
    r1 = float(centred[:-1] @ centred[1:]) / denominator if denominator > 0.0 else 0.0
    r = 2.0 * math.sin(math.pi * r1 / 6.0)
    phi = r + (AR1_BIAS_CONSTANT + AR1_BIAS_SLOPE * r) / n
    return r, min(max(phi, 0.0), AR1_MAX)


def ar1_factor(n: int, phi: float) -> float:
    """``n/n*`` of the Hamed-Rao formula with ``rho_k = phi^k`` at every lag (at least 1 for
    ``phi >= 0``)."""
    if n < MIN_POINTS:
        raise ValueError(f"the AR(1) factor needs at least {MIN_POINTS} points")
    k = np.arange(1, n - 2, dtype=np.float64)  # (n-k)(n-k-1)(n-k-2) is zero from k = n-2 on
    weights = (n - k) * (n - k - 1.0) * (n - k - 2.0)
    return 1.0 + 2.0 * float(np.sum(weights * phi**k)) / (n * (n - 1.0) * (n - 2.0))


def slope_interval(slopes: FloatArray, var_s: float, alpha: float) -> tuple[float, float]:
    """Sen's (1968) confidence interval for the median of the ascending ``slopes``."""
    count = int(slopes.size)
    c_alpha = NormalDist().inv_cdf(1.0 - alpha / 2.0) * math.sqrt(max(var_s, 0.0))
    return (
        _at_rank(slopes, (count - c_alpha) / 2.0),
        _at_rank(slopes, (count + c_alpha) / 2.0 + 1.0),
    )


def _at_rank(sorted_values: FloatArray, rank: float) -> float:
    """The value at a 1-based, possibly fractional rank, interpolated and clipped to the ends."""
    rank = min(max(rank, 1.0), float(sorted_values.size))
    below = math.floor(rank)
    above = math.ceil(rank)
    low = float(sorted_values[below - 1])
    return low + (rank - below) * (float(sorted_values[above - 1]) - low)


def trend(
    series: Series, *, alpha: float = DEFAULT_ALPHA, correction: Correction = Correction.AR1
) -> TrendResult:
    """Theil-Sen slope and Mann-Kendall test of ``series`` at level ``alpha``."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    n = series.n
    if n < MIN_POINTS:
        raise ValueError(f"a trend needs at least {MIN_POINTS} points, got {n}")
    if n > MAX_POINTS:
        raise ValueError(f"at most {MAX_POINTS} points: take a window, got {n}")
    if bool((np.diff(series.t) <= 0).any()):
        raise ValueError("a trend needs distinct times: aggregate to one value per day first")

    s, slopes = pairwise(series.t, series.y)
    var_s = mann_kendall_variance(series.y)
    slope = float(np.median(slopes))
    t_ref = float(series.t[0])
    intercept = float(np.median(series.y - slope * (series.t - t_ref)))

    raw, lags = 1.0, ()
    ar1_r: float | None = None
    ar1_phi: float | None = None
    if correction is not Correction.NONE:
        residual_ranks = rankdata(series.y - slope * (series.t - t_ref), method="average")
        ranks = np.asarray(residual_ranks, dtype=np.float64)
        if correction is Correction.HAMED_RAO:
            raw, lags = hamed_rao_factor(ranks)
        else:
            ar1_r, ar1_phi = ar1_coefficient(ranks)
            raw = ar1_factor(n, ar1_phi)
    factor = max(1.0, raw)
    var_used = var_s * factor

    z = mann_kendall_z(s, var_used)
    p_value = two_sided_p(z)
    significant = p_value < alpha
    direction = Direction.NONE
    if significant:
        direction = Direction.INCREASING if s > 0 else Direction.DECREASING
    low, high = slope_interval(slopes, var_used, alpha)
    return TrendResult(
        s=s,
        tau=s / (n * (n - 1) / 2),
        var_s=var_s,
        n_over_n_star_raw=raw,
        n_over_n_star=factor,
        var_s_used=var_used,
        significant_lags=lags,
        ar1_r=ar1_r,
        ar1_phi=ar1_phi,
        z=z,
        p_value=p_value,
        alpha=alpha,
        significant=significant,
        direction=direction,
        slope=slope,
        slope_low=low,
        slope_high=high,
        intercept=intercept,
        t_ref=t_ref,
        correction=correction,
        inputs=series.summary(),
    )
