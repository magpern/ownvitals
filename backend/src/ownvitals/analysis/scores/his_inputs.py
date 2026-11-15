"""The HIS criteria's inputs (ADR 0012 §2): plain values in, ``CriterionInput`` out. Pure.

Each reducer takes the points of one stream (``(day, value)``, ``None`` value = the field was
not reported), the evaluation ``day`` and applies the criterion's window and minimum. A day
without a record is never 0: it is simply not a point. Inputs are the quality-gated usable
readings, ``attrib=1`` excluded, chosen by the caller.
"""

import math
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from statistics import median
from typing import Final, Literal

from ownvitals.analysis.scores import Status
from ownvitals.analysis.scores.his import CriterionInput

type Point = tuple[date, float | None]

BP_CUTS: Final = {"eu": (135.0, 85.0), "us": (130.0, 80.0)}  # systolic >=, diastolic >=


def _window(day: date, days: int) -> dict[str, str | int]:
    return {
        "start": (day - timedelta(days=days - 1)).isoformat(),
        "end": day.isoformat(),
        "days": days,
    }


def _values(points: Iterable[Point], day: date, days: int) -> list[float]:
    start = day - timedelta(days=days - 1)
    return [v for d, v in points if v is not None and start <= d <= day]


def _short(unit: str, day: date, days: int, n: int, need: int, what: str) -> CriterionInput:
    return CriterionInput(
        Status.INSUFFICIENT_DATA,
        unit=unit,
        reason="insufficient_data",
        window=_window(day, days),
        data_quality={"n": n, "required": need, what: n},
    )


def _done(value: float, unit: str, day: date, days: int, n: int) -> CriterionInput:
    return CriterionInput(
        Status.COMPUTED, value, unit, window=_window(day, days), data_quality={"n": n}
    )


def median_criterion(
    points: Iterable[Point], day: date, *, days: int, minimum: int, unit: str
) -> CriterionInput:
    """The median of the points in the ``days`` ending ``day`` (``fat_ratio``, ``night_hr``,
    ``daily_steps``, ``sleep_ahi``). No point at all: ``not_available``."""
    pts = list(points)
    values = _values(pts, day, days)
    if not any(v is not None for _, v in pts):
        return CriterionInput(
            Status.NOT_AVAILABLE, unit=unit, reason="no_data", window=_window(day, days)
        )
    if len(values) < minimum:
        return _short(unit, day, days, len(values), minimum, "days")
    return _done(float(median(values)), unit, day, days, len(values))


# Height in metres: an engineering bound, not a health range. Inclusive; a value outside it (or not
# finite) is a typo or a unit mix-up and is never used.
HEIGHT_M_MIN: Final = 0.5
HEIGHT_M_MAX: Final = 2.6


def pick_height(candidates: Iterable[tuple[str, float]]) -> tuple[tuple[str, float] | None, bool]:
    """``(newest in-bound height, any rejected)`` from confirmed heights, newest first. A bad newer
    reading never hides a good older one; ``rejected`` says an out-of-bound one existed."""
    rejected = False
    for when, metres in candidates:
        if math.isfinite(metres) and HEIGHT_M_MIN <= metres <= HEIGHT_M_MAX:
            return (when, metres), rejected
        rejected = True
    return None, rejected


def bmi(
    weight: Iterable[Point], height_m: float | None, day: date, height_rejected: bool = False
) -> CriterionInput:
    if height_m is None:
        reason = "implausible_height" if height_rejected else "no_height"
        return CriterionInput(Status.NOT_AVAILABLE, unit="kg/m2", reason=reason)
    c = median_criterion(weight, day, days=30, minimum=4, unit="kg/m2")
    if c.status is not Status.COMPUTED or c.value is None:
        return c
    return CriterionInput(
        c.status, c.value / (height_m * height_m), c.unit, None, c.window, c.data_quality
    )


def vascular_age_gap(vascular_age: Iterable[Point], age: int | None, day: date) -> CriterionInput:
    c = median_criterion(vascular_age, day, days=90, minimum=2, unit="years")
    if c.status is not Status.COMPUTED or c.value is None or age is None:
        return c
    return CriterionInput(c.status, c.value - age, c.unit, None, c.window, c.data_quality)


def afib_ecg_count(
    recordings: Sequence[tuple[date, bool]], day: date, *, ecg_supported: bool
) -> CriterionInput:
    """Recordings in the 365 days ending ``day`` that meet E1's condition by Withings'
    classification (the flag); at least 3 recordings in the window."""
    if not ecg_supported:
        return CriterionInput(Status.NOT_SUPPORTED, unit="recordings", reason="ecg_not_supported")
    start = day - timedelta(days=364)
    inside = [meets for d, meets in recordings if start <= d <= day]
    if len(inside) < 3:
        return _short("recordings", day, 365, len(inside), 3, "recordings")
    return _done(float(sum(inside)), "recordings", day, 365, len(inside))


def high_bp_ratio(
    groups: Sequence[tuple[date, float | None, float | None]],
    day: date,
    *,
    variant: Literal["eu", "us"] = "eu",
) -> CriterionInput:
    """Share of blood-pressure groups in 90 days with systolic or diastolic at or above the cut;
    a group with only one of the two values is not counted."""
    start = day - timedelta(days=89)
    full = [(s, d) for g, s, d in groups if s is not None and d is not None and start <= g <= day]
    if not groups:
        return CriterionInput(Status.NOT_AVAILABLE, unit="ratio", reason="no_data")
    if len(full) < 6:
        return _short("ratio", day, 90, len(full), 6, "groups")
    sys_cut, dia_cut = BP_CUTS[variant]
    high = sum(1 for s, d in full if s >= sys_cut or d >= dia_cut)
    return _done(high / len(full), "ratio", day, 90, len(full))


def weekly_active_minutes(days_present: Iterable[tuple[date, float]], day: date) -> CriterionInput:
    """Minutes per 7 days, scaled from the days with a record in the 28 days ending ``day``:
    a missing day never counts as 0 minutes (a record of 0 does)."""
    start = day - timedelta(days=27)
    records = list(days_present)
    minutes = [m for d, m in records if start <= d <= day]
    if not records:
        return CriterionInput(Status.NOT_AVAILABLE, unit="min/week", reason="no_data")
    if len(minutes) < 20:
        return _short("min/week", day, 28, len(minutes), 20, "days")
    return _done(sum(minutes) / len(minutes) * 7, "min/week", day, 28, len(minutes))
