"""Builders for the nerve and system rule tests. SYNTHETIC values only: hand-written and seeded
series, never a real reading (AGENTS.md)."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count

from rules_builders import dv

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.quality import Reading

SYNTHETIC = True
GUIDED = 15
_grpids = count(5000)


def nreading(
    day: date, value: float | str, *, attrib: int = 0, hour: int = 8, grpid: int | None = None
) -> Reading:
    return Reading(
        grpid=next(_grpids) if grpid is None else grpid,
        measured_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
        source_tz="UTC",
        attrib=attrib,
        value=Decimal(str(value)),
    )


def weekly(last: date, count_: int, value: float | str, *, attrib: int = 0) -> list[Reading]:
    """``count_`` readings of ``value`` every 7 days, the last on ``last``."""
    return [nreading(last - timedelta(days=7 * i), value, attrib=attrib) for i in range(count_)]


def daily_readings(last: date, values: Sequence[float | str], *, attrib: int = 0) -> list[Reading]:
    """One reading per consecutive day, the last on ``last``."""
    n = len(values)
    return [
        nreading(last - timedelta(days=n - 1 - i), v, attrib=attrib) for i, v in enumerate(values)
    ]


def weekly_daily(last: date, values: Sequence[float]) -> list[DailyValue]:
    """Daily values one week apart, the last on ``last``."""
    n = len(values)
    return [dv(last - timedelta(days=7 * (n - 1 - i)), round(v, 2)) for i, v in enumerate(values)]


def foot_group(day: date, left: float | str, right: float | str) -> tuple[Reading, Reading]:
    """Two readings of one measure group (one ``grpid``), one per member of a foot pair."""
    grpid = next(_grpids)
    return nreading(day, left, grpid=grpid), nreading(day, right, grpid=grpid)
