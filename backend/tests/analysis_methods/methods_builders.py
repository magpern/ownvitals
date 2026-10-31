"""Builders for the method tests. SYNTHETIC data only: seeded random numbers and hand-written
arrays, never a real reading (AGENTS.md)."""

import math
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from functools import cache

import numpy as np
from numpy.typing import NDArray
from scipy.signal import lfilter

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.methods.series import Series
from ownvitals.analysis.methods.trend import (
    Correction,
    TrendResult,
    mann_kendall_z,
    trend,
    two_sided_p,
)

SYNTHETIC = True  # every series in these tests is generated or written by hand

START = date(2026, 1, 1)


def generator(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def ar1(rng: np.random.Generator, n: int, phi: float, burn: int = 50) -> NDArray[np.float64]:
    """A stationary AR(1) series ``x_i = phi * x_(i-1) + e_i`` with standard normal ``e``."""
    noise = rng.normal(size=n + burn)
    return np.asarray(lfilter([1.0], [1.0, -phi], noise)[burn:], dtype=np.float64)


def irregular_times(
    rng: np.random.Generator, n: int, span: float, *, with_gaps: bool = False
) -> NDArray[np.float64]:
    """``n`` distinct, sorted times in ``[0, span)``; with ``with_gaps`` a stretch of the span
    is left empty (a holiday, a broken scale) and the points are clustered around it."""
    times = rng.uniform(0.0, span, size=n)
    if with_gaps:
        hole_start, hole_end = 0.4 * span, 0.6 * span
        inside = (times >= hole_start) & (times < hole_end)
        times[inside] = hole_end + (times[inside] - hole_start) * 0.1
    return np.unique(np.round(times, 3))


def daily_values(offsets: Sequence[int], values: Sequence[float]) -> list[DailyValue]:
    """One daily value per day offset from ``START``, each a single reading."""
    return [
        DailyValue(
            day=START + timedelta(days=offset),
            value=Decimal(str(value)),
            method=DailyMethod.MEDIAN,
            n=1,
            minimum=Decimal(str(value)),
            maximum=Decimal(str(value)),
            mad=Decimal(0),
        )
        for offset, value in zip(offsets, values, strict=True)
    ]


# --- seeded error-rate simulations of the trend test --------------------------------------------

RUNS = 1000


def binomial_slack(level: float, runs: int = RUNS) -> float:
    """Three standard errors of an observed rate of a test with true rate ``level``."""
    return 3.0 * math.sqrt(level * (1.0 - level) / runs)


def null_trend_series(rng: np.random.Generator, n: int, phi: float) -> Series:
    """``n`` values with no trend at all (white noise for ``phi`` 0, else AR(1)) at irregular
    times: the test uses the order, the detrending the times."""
    values = ar1(rng, n, phi) if phi else rng.normal(size=n)
    times = np.sort(rng.choice(np.arange(int(n * 1.6)), size=n, replace=False)).astype(float)
    return Series.from_arrays(times, values)


def plain_p_value(result: TrendResult) -> float:
    """The p-value of the plain Mann-Kendall test, from ``S`` and ``Var(S)`` of any result."""
    return two_sided_p(mann_kendall_z(result.s, result.var_s))


@cache
def p_values(correction: Correction, n: int, phi: float, runs: int, seed: int) -> np.ndarray:
    """The p-values of ``runs`` seeded series without a trend (see ``null_trend_series``)."""
    rng = generator(seed)
    found = np.empty(runs)
    for i in range(runs):
        found[i] = trend(null_trend_series(rng, n, phi), correction=correction).p_value
    return found


@cache
def plain_and_ar1_p_values(
    n: int, phi: float, runs: int, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """The p-values of the plain and of the AR(1)-corrected test on the same ``runs`` series
    without a trend. One call per series gives both: the plain test is ``S`` and ``Var(S)`` of
    the result (``test_plain_p_value_is_that_of_the_none_correction`` checks it)."""
    rng = generator(seed)
    plain = np.empty(runs)
    corrected = np.empty(runs)
    for i in range(runs):
        result = trend(null_trend_series(rng, n, phi))
        plain[i] = plain_p_value(result)
        corrected[i] = result.p_value
    return plain, corrected
