"""Exponentially weighted moving average weighted by the **time gap** (PLAN §7.1 step 5). Pure.

An EWMA with a fixed smoothing factor per *point* treats a reading after a three-week gap like
one after a day. Here the weight of a new reading depends on the elapsed time ``Δ`` since the
previous one, through a half-life ``h`` (days):

    alpha_i = 1 - 2^(-Δ_i / h)
    E_0 = y_0
    E_i = alpha_i · y_i + (1 - alpha_i) · E_(i-1)

so a reading after ``h`` days weighs one half, after a long gap it weighs nearly one (the past
has been forgotten), and two readings a day apart weigh the same whether or not other readings
lie between them. This is the exponential moving average of an unevenly spaced series with
"next-point" interpolation (the series is taken to hold the value of the next observation until
it arrives), as in Eckner, "Algorithms for Unevenly-Spaced Time Series: Moving Averages and Other
Rolling Operators" (2019), §3.1. Its consistency property, used in the tests: adding a point
that repeats the value of the *next* point changes nothing, because
``(1 - alpha(Δ1)) · (1 - alpha(Δ2)) = 1 - alpha(Δ1 + Δ2)``.

* The first point initialises the average, so early values lean on the first reading until about
  one half-life has passed. Treat the first few half-lives as a warm-up.
* Points at the same time are merged into their mean first (``Δ = 0`` would otherwise give the
  later one no weight), so the result does not depend on their order; each of them gets the
  value of the merged point.
* A series that is not sorted must go through ``Series.from_arrays``.
"""

from dataclasses import dataclass
from typing import Any

import numpy as np

from ownvitals.analysis.methods.series import FloatArray, InputSummary, Series, list_or_none

ALGO_VERSION = "ewma/1"


@dataclass(frozen=True, slots=True, eq=False)
class EwmaResult:
    """``ewma[i]`` and ``alpha[i]`` belong to the series' point ``i`` (``alpha[0]`` is 1)."""

    t: FloatArray
    ewma: FloatArray
    alpha: FloatArray  # weight of the point against the average before it
    halflife_days: float
    inputs: InputSummary
    algo_version: str = ALGO_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "t": list_or_none(self.t),
            "ewma": list_or_none(self.ewma),
            "alpha": list_or_none(self.alpha),
            "halflife_days": self.halflife_days,
            "inputs": self.inputs.as_dict(),
            "algo_version": self.algo_version,
        }


def gap_weight(gap_days: FloatArray, halflife_days: float) -> FloatArray:
    """The weight of a reading ``gap_days`` after the previous one: ``1 - 2^(-gap/halflife)``."""
    return np.asarray(-np.expm1(-gap_days * (np.log(2.0) / halflife_days)), dtype=np.float64)


def ewma(series: Series, *, halflife_days: float) -> EwmaResult:
    """The gap-weighted EWMA at each point of ``series``."""
    if not halflife_days > 0:
        raise ValueError("halflife_days must be positive")
    if series.n == 0:
        empty = np.empty(0)
        return EwmaResult(empty, empty, empty, float(halflife_days), series.summary())

    times, inverse = np.unique(series.t, return_inverse=True)
    sums = np.bincount(inverse, weights=series.y, minlength=times.size)
    counts = np.bincount(inverse, minlength=times.size)
    merged = sums / counts

    alphas = np.ones(times.size)
    alphas[1:] = gap_weight(np.diff(times), halflife_days)
    average = np.empty(times.size)
    average[0] = merged[0]
    for i in range(1, times.size):
        average[i] = alphas[i] * merged[i] + (1.0 - alphas[i]) * average[i - 1]
    return EwmaResult(
        t=np.array(series.t),
        ewma=average[inverse],
        alpha=alphas[inverse],
        halflife_days=float(halflife_days),
        inputs=series.summary(),
    )
