"""Daily-value helpers and provenance of the v1 finding rules (ADR 0010 §1; ADR 0006 §11). Pure.

Split out of ``ownvitals.analysis.rules.core``, which re-exports every name here.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.methods.series import EPOCH


def day_number(day: date) -> int:
    """Days since 1970-01-01: the time axis of ``Series.from_daily``."""
    return (day - EPOCH).days


def window_of(daily: Iterable[DailyValue], end: date, days: int) -> list[DailyValue]:
    """The daily values on the ``days`` calendar days ending on ``end`` (both ends inclusive)."""
    start = end - timedelta(days=days - 1)
    return [v for v in daily if start <= v.day <= end]


def value_on(daily: Iterable[DailyValue], day: date) -> DailyValue | None:
    return next((v for v in daily if v.day == day), None)


def require_value(daily: Iterable[DailyValue], day: date, what: str) -> DailyValue:
    """The trigger's daily value: a rule is evaluated only on a day that has one (§1)."""
    found = value_on(daily, day)
    if found is None:
        raise ValueError(f"{what} has no daily value on {day}: evaluate only days with data")
    return found


# --- provenance ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """What a daily value came from: the measure groups it used (ADR 0006 §11's trigger rule) and
    whether one of its readings carried #37's advisory ``jump`` flag."""

    groups: tuple[int, ...] = ()
    jump: bool = False


type ProvenanceMap = Mapping[str, Mapping[date, Provenance]]


def provenance_of(provenance: ProvenanceMap | None, key: str, day: date) -> Provenance:
    return (provenance or {}).get(key, {}).get(day, Provenance())


def groups_on(provenance: ProvenanceMap | None, key: str, days: Iterable[date]) -> list[int]:
    found = {g for day in days for g in provenance_of(provenance, key, day).groups}
    return sorted(found)
