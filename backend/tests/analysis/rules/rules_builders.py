"""Builders for the rule tests. SYNTHETIC values only: hand-written and seeded series, never a
real reading (AGENTS.md)."""

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count

import numpy as np
from numpy.typing import NDArray
from scipy.signal import lfilter

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules.core import Confidence, Finding, make_finding

SYNTHETIC = True

DAY = date(2026, 6, 30)  # the evaluation day of most tests
_grpids = count(1000)


def dv(day: date, value: float | str | Decimal, *, n: int = 1) -> DailyValue:
    v = Decimal(str(value))
    return DailyValue(day, v, DailyMethod.MORNING, n, v, v, Decimal(0))


def run_of(first: date, values: Iterable[float | str]) -> list[DailyValue]:
    """One daily value per consecutive day from ``first``."""
    return [dv(first + timedelta(days=i), v) for i, v in enumerate(values)]


def ending(last: date, values: Sequence[float | str]) -> list[DailyValue]:
    """Consecutive daily values, the last on ``last``."""
    return run_of(last - timedelta(days=len(values) - 1), values)


def alternating(first: date, days: int, centre: float, spread: float) -> list[DailyValue]:
    """``days`` consecutive days alternating centre -/+ spread: median ``centre``, raw MAD
    ``spread`` exactly (an even count)."""
    return run_of(first, [centre + (spread if i % 2 else -spread) for i in range(days)])


def constant(first: date, days: int, value: float | str) -> list[DailyValue]:
    return run_of(first, [value] * days)


def preceding(q: date, values: Sequence[float | str]) -> list[DailyValue]:
    """Daily values on the days right before ``q`` (the last on ``q - 1``)."""
    return ending(q - timedelta(days=1), values)


def noise(seed: int, n: int, phi: float = 0.0) -> NDArray[np.float64]:
    rng = np.random.default_rng(seed)
    return np.asarray(lfilter([1.0], [1.0, -phi], rng.normal(size=n + 50))[50:], dtype=np.float64)


def reading(day: date, value: float | str, *, grpid: int | None = None, hour: int = 8) -> Reading:
    """A reading on ``day`` in UTC; hour 8 is a morning reading, 20 is not."""
    return Reading(
        grpid=next(_grpids) if grpid is None else grpid,
        measured_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
        source_tz="UTC",
        attrib=0,
        value=Decimal(str(value)),
    )


def fired(
    day: date, *, rule: str = "B5", subject: str = "visceral_fat", window_days: int = 90
) -> Finding:
    return make_finding(
        rule,
        subject,
        day,
        window_start=day - timedelta(days=window_days - 1),
        window_end=day,
        reference_day=day,
        confidence=Confidence.HIGH,
        evidence={},
        data_quality=None,
    )
