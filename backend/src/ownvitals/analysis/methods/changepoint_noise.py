"""Noise estimation for the change-point search (PLAN §7.1 step 5). Pure.

The robust long-run noise level ``sigma`` and AR(1) coefficient ``phi`` that scale the PELT
cost, estimated from the variogram of the differences of the series (see the module docstring of
``ownvitals.analysis.methods.changepoint``, which re-exports every name here).
"""

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

from ownvitals.analysis.methods.series import MAD_SCALE, FloatArray, mad

PHI_MAX = 0.7  # the largest φ used; more persistent noise is treated as level changes
PHI_MARGIN = 1.0  # φ is raised by PHI_MARGIN / √n: the estimate from differences is noisy
MAX_LAG = 5  # the variogram is fitted over the lags 1 to MAX_LAG
PHI_GRID = np.linspace(0.0, PHI_MAX, 71)  # the φ the fit searches: steps of 0.01
TRIM_CUTOFF = 3.0  # differences beyond this many robust standard deviations are left out
# variance of a standard normal truncated to ±TRIM_CUTOFF: what the variance of the rest is
# divided by
TRIM_VARIANCE = 1.0 - 2.0 * TRIM_CUTOFF * NormalDist().pdf(TRIM_CUTOFF) / (
    2.0 * NormalDist().cdf(TRIM_CUTOFF) - 1.0
)
MIN_POINTS_FOR_PHI = 4  # fewer have only one or two differences at lag 2


def estimate_sigma(values: FloatArray) -> float:
    """The robust noise level of **independent** noise from the first differences,
    ``1.4826 · MAD(Δy) / √2`` (the standard deviation of the differences over √2 if more than half
    of them are zero); 0 for a constant series. ``estimate_long_run`` is the default."""
    if values.size < 2:
        return 0.0
    differences = np.diff(values)
    sigma = MAD_SCALE * mad(differences) / math.sqrt(2.0)
    if sigma == 0.0:
        sigma = float(np.std(differences)) / math.sqrt(2.0)
    return sigma


def trimmed_variance(values: FloatArray, cutoff: float = TRIM_CUTOFF) -> float:
    """The variance of ``values`` without those beyond ``cutoff`` robust standard deviations of
    the median, corrected for the truncation of a normal distribution (for ``cutoff`` =
    ``TRIM_CUTOFF``). The plain variance if more than half of the values equal the median."""
    scale = MAD_SCALE * mad(values)
    if scale == 0.0:
        return float(np.var(values))
    kept = values[np.abs(values - np.median(values)) <= cutoff * scale]
    return float(np.var(kept)) / TRIM_VARIANCE


@dataclass(frozen=True, slots=True)
class NoiseEstimate:
    phi: float  # the AR(1) coefficient used (0 to PHI_MAX)
    sigma: float  # the long-run standard deviation, in the units of the values


def fit_variogram(variances: FloatArray) -> tuple[float, float]:
    """``(phi, g)`` of the AR(1) variogram ``v_h = 2 g (1 - phi^h)`` that fits ``variances``
    (``v_1`` to ``v_H``) best in least squares, ``phi`` from ``PHI_GRID`` (the smallest if several
    fit equally well). For a given ``phi`` the best ``2 g`` is ``sum(v w) / sum(w^2)`` with
    ``w_h = 1 - phi^h``."""
    lags = np.arange(1, variances.size + 1, dtype=np.float64)
    weights = 1.0 - PHI_GRID[:, None] ** lags[None, :]
    levels = (weights @ variances) / np.sum(weights * weights, axis=1)
    errors = np.sum((variances[None, :] - levels[:, None] * weights) ** 2, axis=1)
    best = int(np.argmin(errors))
    return float(PHI_GRID[best]), float(levels[best]) / 2.0


def estimate_long_run(values: FloatArray) -> NoiseEstimate:
    """The AR(1) coefficient and long-run standard deviation of the noise of ``values``, from the
    robust variogram of its differences (see the module docstring); ``sigma`` is 0 for a constant
    series."""
    n = int(values.size)
    if n < MIN_POINTS_FOR_PHI:
        return NoiseEstimate(phi=0.0, sigma=estimate_sigma(values))
    variances = np.array(
        [trimmed_variance(values[h:] - values[:-h]) for h in range(1, min(MAX_LAG, n - 2) + 1)]
    )
    if variances[0] == 0.0:
        return NoiseEstimate(phi=0.0, sigma=0.0)
    fitted, marginal_variance = fit_variogram(variances)
    phi = min(fitted + PHI_MARGIN / math.sqrt(n), PHI_MAX)
    return NoiseEstimate(phi=phi, sigma=math.sqrt(marginal_variance * (1.0 + phi) / (1.0 - phi)))
