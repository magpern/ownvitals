"""Builders for the method tests. SYNTHETIC data only: seeded random numbers and hand-written
arrays, never a real reading (AGENTS.md)."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray
from scipy.signal import lfilter

from ownvitals.analysis.daily import DailyMethod, DailyValue

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
