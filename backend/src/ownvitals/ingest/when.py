"""Which instants may be stored, and the zone used to name a local day.

A row dated outside [2009-01-01, now + 3 months) is not written. The caller counts it and
records the reason; the value and the timestamp are not logged. ``source_tz`` is an IANA name
from the ``tzdata`` package, so day assignment does not depend on the host's zone database.
An unknown name is stored as UTC: the instant itself stays the UTC timestamp the API sent.
"""

import calendar
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FLOOR = datetime(2009, 1, 1, tzinfo=UTC)


def plus_months(moment: datetime, months: int) -> datetime:
    index = moment.month - 1 + months
    year = moment.year + index // 12
    month = index % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def in_range(instant: datetime, now: datetime) -> bool:
    if instant.tzinfo is None:
        return False
    return FLOOR <= instant.astimezone(UTC) < plus_months(now, 3)


def month_of(instant: datetime) -> date:
    utc = instant.astimezone(UTC)
    return date(utc.year, utc.month, 1)


def zone_name(name: str | None, fallback: str = "UTC") -> str:
    candidate = name or fallback or "UTC"
    if not 1 <= len(candidate) <= 64:
        candidate = "UTC"
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return "UTC"
    return candidate


def from_epoch(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def local_day(instant: datetime, zone: str) -> date:
    return instant.astimezone(ZoneInfo(zone)).date()
