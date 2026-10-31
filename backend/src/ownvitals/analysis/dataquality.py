"""The ``data_quality`` summary every finding carries (PLAN §7.1 step 6). Pure.

It describes the daily values inside a window of ``window_days`` calendar days ending on
``window_end`` (inclusive; the first day is ``window_end - (window_days - 1)``):

* ``n_readings`` and ``n_days``: usable readings and days with data in the window;
* ``coverage``: ``n_days / window_days``, the share of the window's days that have data;
* ``span_days``: from the first to the last day with data, both ends counted
  (``(last - first).days + 1``); ``span_fraction``: ``span_days / window_days``;
* ``gaps``: runs of consecutive days without data inside the window (including at its edges) of
  at least ``min_gap_days`` days; ``longest_gap_days`` covers every run;
* measurement frequency: ``readings_per_week`` over the window and ``median_days_between``,
  the median distance between consecutive days with data.

Ratios are exact ``Fraction`` values; ``as_dict`` turns them into rounded floats for JSON.
"""

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from itertools import pairwise
from typing import Any

from ownvitals.analysis.daily import DailyValue

ALGO_VERSION = "dataquality/1"

DEFAULT_MIN_GAP_DAYS = 3


@dataclass(frozen=True, slots=True)
class Gap:
    """Consecutive days without data, ``start`` to ``end`` inclusive."""

    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True, slots=True)
class DataQuality:
    window_start: date
    window_end: date
    window_days: int
    n_readings: int
    n_days: int
    first_day: date | None
    last_day: date | None
    span_days: int
    coverage: Fraction
    span_fraction: Fraction
    min_gap_days: int
    longest_gap_days: int
    gaps: tuple[Gap, ...]
    readings_per_week: Fraction
    median_days_between: Fraction | None
    algo_version: str = ALGO_VERSION

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form (dates as ISO strings, ratios as floats rounded to 4 places)."""
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "window_days": self.window_days,
            "n_readings": self.n_readings,
            "n_days": self.n_days,
            "first_day": self.first_day.isoformat() if self.first_day else None,
            "last_day": self.last_day.isoformat() if self.last_day else None,
            "span_days": self.span_days,
            "coverage": round(float(self.coverage), 4),
            "span_fraction": round(float(self.span_fraction), 4),
            "min_gap_days": self.min_gap_days,
            "longest_gap_days": self.longest_gap_days,
            "gaps": [
                {"start": g.start.isoformat(), "end": g.end.isoformat(), "days": g.days}
                for g in self.gaps
            ],
            "readings_per_week": round(float(self.readings_per_week), 4),
            "median_days_between": (
                None
                if self.median_days_between is None
                else round(float(self.median_days_between), 4)
            ),
            "algo_version": self.algo_version,
        }


def _missing_runs(days: list[date], start: date, end: date) -> list[Gap]:
    """Maximal runs of days without data in ``[start, end]``; ``days`` sorted, inside it."""
    runs: list[Gap] = []
    cursor = start
    for day in days:
        if day > cursor:
            runs.append(Gap(cursor, day - timedelta(days=1)))
        cursor = day + timedelta(days=1)
    if cursor <= end:
        runs.append(Gap(cursor, end))
    return runs


def summarise(
    daily: Iterable[DailyValue],
    *,
    window_end: date,
    window_days: int,
    min_gap_days: int = DEFAULT_MIN_GAP_DAYS,
) -> DataQuality:
    """Data quality of ``daily`` inside the window. Days outside it are ignored; two daily
    values of one day are a ``ValueError``."""
    if window_days < 1:
        raise ValueError("window_days must be at least 1")
    if min_gap_days < 1:
        raise ValueError("min_gap_days must be at least 1")
    window_start = window_end - timedelta(days=window_days - 1)
    inside: dict[date, DailyValue] = {}
    for value in daily:
        if not window_start <= value.day <= window_end:
            continue
        if value.day in inside:
            raise ValueError(f"two daily values for {value.day}")
        inside[value.day] = value
    days = sorted(inside)
    runs = _missing_runs(days, window_start, window_end)
    n_readings = sum(v.n for v in inside.values())
    span_days = (days[-1] - days[0]).days + 1 if days else 0
    between = [Fraction((b - a).days) for a, b in pairwise(days)]
    return DataQuality(
        window_start=window_start,
        window_end=window_end,
        window_days=window_days,
        n_readings=n_readings,
        n_days=len(days),
        first_day=days[0] if days else None,
        last_day=days[-1] if days else None,
        span_days=span_days,
        coverage=Fraction(len(days), window_days),
        span_fraction=Fraction(span_days, window_days),
        min_gap_days=min_gap_days,
        longest_gap_days=max((g.days for g in runs), default=0),
        gaps=tuple(g for g in runs if g.days >= min_gap_days),
        readings_per_week=Fraction(7 * n_readings, window_days),
        median_days_between=statistics.median(between) if between else None,
    )
