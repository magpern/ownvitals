"""Autocorrelation corrections of the Mann-Kendall variance (PLAN §7.1 step 5). Pure.

The Hamed-Rao (1998) and AR(1) inflation factors ``n/n*`` and their constants, split out of
``ownvitals.analysis.methods.trend`` (whose module docstring explains them and which re-exports
every name here).
"""

import math
from statistics import NormalDist

import numpy as np

from ownvitals.analysis.methods.series import FloatArray

MIN_POINTS = 3
ACF_ALPHA = 0.05  # the level at which Hamed and Rao call an autocorrelation significant
AR1_BIAS_CONSTANT = 6.0  # phi = r + (AR1_BIAS_CONSTANT + AR1_BIAS_SLOPE * r) / n, see the docstring
AR1_BIAS_SLOPE = 6.0
AR1_MAX = 0.95  # the largest phi used: 1 would be a random walk, with no finite variance


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
