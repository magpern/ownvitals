"""Daily value (PLAN §7.1 step 3). Pure and deterministic.

A reading belongs to the calendar day of its own ``source_tz`` (``Reading.local_date``), so a
trip across time zones, a DST change or a reading just before or after local midnight never
needs a special case: the zone rules turn the instant into a local date and time.

The daily value of a day is the **first morning reading** (the earliest instant whose local time
is in ``[04:00, 11:00)`` in the reading's own zone), otherwise the **median** of the day's
readings. ``n`` and the spread (min, max and the raw, unscaled MAD of all of the day's readings,
not only the morning ones) are kept. All arithmetic is exact ``Decimal``.

Pass ``QualityResult.usable``: this module does not apply the quality gates.
"""

import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal, localcontext
from enum import StrEnum

from ownvitals.analysis.quality import EXACT, Reading

ALGO_VERSION = "daily/1"

MORNING_START = time(4, 0)  # inclusive
MORNING_END = time(11, 0)  # exclusive


class DailyMethod(StrEnum):
    MORNING = "morning"
    MEDIAN = "median"


@dataclass(frozen=True, slots=True)
class DailyValue:
    day: date
    value: Decimal
    method: DailyMethod
    n: int
    minimum: Decimal
    maximum: Decimal
    mad: Decimal
    algo_version: str = ALGO_VERSION


def is_morning(reading: Reading) -> bool:
    """Local time of day in ``[04:00, 11:00)`` in the reading's own zone."""
    return MORNING_START <= reading.local.time() < MORNING_END


def _daily_value(day: date, readings: list[Reading]) -> DailyValue:
    ordered = sorted(readings, key=lambda r: r.sort_key)
    values = [r.value for r in ordered]
    morning = next((r for r in ordered if is_morning(r)), None)
    centre = statistics.median(values)
    return DailyValue(
        day=day,
        value=centre if morning is None else morning.value,
        method=DailyMethod.MEDIAN if morning is None else DailyMethod.MORNING,
        n=len(values),
        minimum=min(values),
        maximum=max(values),
        mad=statistics.median(abs(v - centre) for v in values),
    )


def daily_values(readings: Iterable[Reading]) -> tuple[DailyValue, ...]:
    """One ``DailyValue`` per local day that has a reading, ascending by day.

    Readings of one metric only, one per ``grpid``: a repeated ``grpid`` is a ``ValueError``
    (``assess`` flags duplicates; pass its ``usable`` readings).
    """
    by_day: dict[date, list[Reading]] = defaultdict(list)
    grpids: set[int] = set()
    for reading in readings:
        if reading.grpid in grpids:
            raise ValueError(f"grpid {reading.grpid} appears twice: pass QualityResult.usable")
        grpids.add(reading.grpid)
        by_day[reading.local_date].append(reading)
    with localcontext(EXACT):
        return tuple(_daily_value(day, by_day[day]) for day in sorted(by_day))
