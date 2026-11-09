"""Builders for the score tests. SYNTHETIC values only (``SYNTHETIC``): hand-written and seeded."""

from datetime import date, timedelta
from decimal import Decimal

from ownvitals.analysis.daily import DailyMethod, DailyValue

SYNTHETIC = True
DAY = date(2026, 6, 15)


def dv(day: date, value: float) -> DailyValue:
    v = Decimal(repr(float(value)))
    return DailyValue(day, v, DailyMethod.MORNING, 1, v, v, Decimal(0))


def line(last: date, values: list[float]) -> list[DailyValue]:
    """Consecutive daily values, the last on ``last``."""
    first = last - timedelta(days=len(values) - 1)
    return [dv(first + timedelta(days=i), v) for i, v in enumerate(values)]
