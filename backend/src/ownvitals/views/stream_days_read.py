"""The per-day values of the sleep and activity streams (ADR 0014 section 4's ``sleep_summary``
and ``activity_summary``; #401), used by ``views.reads.Reader`` inside its ``user_session``
(forced RLS; ``owner_id`` filtered too).

It reads the normalised stream observations (``derived.normalize_streams``: a sleep summary's
fields under ``sleep_``, an activity day's under ``activity_``, ``source_type IS NULL``) with the
scores pass's rules (``derived.scores_gather``): rows flagged ``unconfirmed``, ``implausible`` or
``duplicate`` are left out, a row's day is its local day (the profile's fixed zone, else the
row's own), and of two rows of one key on one day the later one wins. Only days and values
leave this module: no instant, zone, source id or row id.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.derived.daily_db import read_day_tz

# A day of any zone lies within these bounds of its UTC midnight (UTC-12 to UTC+14).
_MARGIN = timedelta(days=1)
_DAYS = text(
    "SELECT metric_key, ts, source_tz, value FROM observations WHERE owner_id = :o "
    "AND metric_key = ANY(CAST(:keys AS text[])) AND source_type IS NULL "
    "AND ts >= :lo AND ts < :hi "
    "AND NOT (quality_flags && ARRAY['unconfirmed','implausible','duplicate']) "
    "ORDER BY metric_key, ts"
)

DayValues = dict[str, dict[date, float]]  # metric key -> day -> value


def _local_day(ts: datetime, zone: str, day_tz: str | None) -> date:
    try:
        return ts.astimezone(ZoneInfo(day_tz or zone)).date()
    except (ValueError, KeyError, OSError):
        return ts.astimezone(UTC).date()


def by_day(rows: Any, day_tz: str | None, first: date, last: date) -> DayValues:
    """Rows ``(metric_key, ts, source_tz, value)`` in ``ts`` order -> each key's value per local
    day in ``[first, last]``, the later row of a day winning."""
    found: DayValues = {}
    for r in rows:
        day = _local_day(r.ts, str(r.source_tz), day_tz)
        if first <= day <= last:
            found.setdefault(r.metric_key, {})[day] = float(r.value)
    return found


async def stream_days(
    db: AsyncSession, owner: UUID, keys: Sequence[str], first: date, last: date
) -> DayValues:
    """The values of ``keys`` per local day from ``first`` to ``last`` (both included)."""
    lo = datetime.combine(first, time(), UTC) - _MARGIN
    hi = datetime.combine(last + timedelta(days=1), time(), UTC) + _MARGIN
    day_tz = await read_day_tz(db, owner)
    rows = (await db.execute(_DAYS, {"o": owner, "keys": list(keys), "lo": lo, "hi": hi})).all()
    return by_day(rows, day_tz, first, last)


__all__ = ["DayValues", "by_day", "stream_days"]
