"""Builders for the method tests. SYNTHETIC data only: seeded random numbers and hand-written
arrays, never a real reading (AGENTS.md)."""

import math
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from functools import cache
from itertools import combinations, pairwise

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


# --- the optimum of a segmentation, two ways to find it, for the PELT tests -------------------


def optimal_partitioning(
    values: np.ndarray, penalty: float, min_segment: int
) -> tuple[tuple[int, ...], float]:
    """Jackson et al. (2005): F(t) = min over s of F(s) + cost(s, t) + penalty, no pruning."""
    n = len(values)
    centred = values - values.mean()

    def cost(a: int, b: int) -> float:
        part = centred[a:b]
        return float(np.sum((part - part.mean()) ** 2))

    best = [math.inf] * (n + 1)
    best[0] = -penalty
    previous = [0] * (n + 1)
    for t in range(min_segment, n + 1):
        for s in [0, *range(min_segment, t - min_segment + 1)]:
            if t - s < min_segment or math.isinf(best[s]):
                continue
            value = best[s] + cost(s, t) + penalty
            if value < best[t] - 1e-12:
                best[t], previous[t] = value, s
    points: list[int] = []
    end = n
    while previous[end] > 0:
        end = previous[end]
        points.append(end)
    points.reverse()
    return tuple(points), best[n] - len(points) * penalty


def best_by_enumeration(
    values: np.ndarray, penalty: float, min_segment: int
) -> tuple[tuple[int, ...], float]:
    """Every way to cut ``values`` into segments of at least ``min_segment`` points (no
    recursion, no pruning): the change points and the cost, without the penalty, of the best."""
    n = len(values)
    best: tuple[tuple[int, ...], float] | None = None
    for count in range(n // min_segment):
        for cuts in combinations(range(min_segment, n - min_segment + 1), count):
            edges = [0, *cuts, n]
            if any(b - a < min_segment for a, b in pairwise(edges)):
                continue
            cost = sum(
                float(np.sum((values[a:b] - values[a:b].mean()) ** 2)) for a, b in pairwise(edges)
            )
            if best is None or cost + penalty * count < best[1] + penalty * len(best[0]) - 1e-12:
                best = (cuts, cost)
    assert best is not None
    return best
