"""Small builders for the analysis tests. Synthetic values only."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import count

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.quality import Reading

STOCKHOLM = "Europe/Stockholm"

_grpids = count(1)


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, us: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, us, tzinfo=UTC)


def reading(
    when: datetime,
    value: str = "80.0",
    *,
    tz: str = STOCKHOLM,
    attrib: int = 0,
    grpid: int | None = None,
) -> Reading:
    return Reading(
        grpid=next(_grpids) if grpid is None else grpid,
        measured_at=when,
        source_tz=tz,
        attrib=attrib,
        value=Decimal(value),
    )


def daily_on(*days: date) -> list[DailyValue]:
    """One single-reading daily value per day."""
    return [
        DailyValue(
            day=d,
            value=Decimal(80),
            method=DailyMethod.MEDIAN,
            n=1,
            minimum=Decimal(80),
            maximum=Decimal(80),
            mad=Decimal(0),
        )
        for d in days
    ]


def spread_days(first: date, span: int, count_: int) -> list[date]:
    """``count_`` distinct days, the first on ``first`` and the last ``span`` days later.

    ``span`` is the days **elapsed** between the first and the last day (what ``span_days``
    counts), so the days cover ``span + 1`` calendar days; ``count_`` is how many of them have
    data."""
    if not 2 <= count_ <= span + 1:
        raise ValueError("need 2 <= count_ <= span + 1")
    middle = list(range(1, span))
    chosen = [0, *middle[: count_ - 2], span]
    return [first + timedelta(days=offset) for offset in chosen]
