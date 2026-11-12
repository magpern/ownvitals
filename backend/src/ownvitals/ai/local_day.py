"""The user's "today" for the AI tools' periods (issue #443).

Recording days are local (the profile's fixed zone, else each reading's own zone), so a period
anchored on ``now``'s UTC date leaves out the last hours of a local day that is ahead of UTC.
``local_today`` is the pure rule; ``today_of`` reads the zone once through ``views.reads`` (forced
RLS, the caller's own rows) and applies it. No zone leaves this module: only a date.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext
from ownvitals.views.reads import reading


def local_today(now: datetime, zone: str | None) -> date:
    """``now``'s date in ``zone``; the UTC date when the zone is absent or unknown."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    try:
        return now.astimezone(ZoneInfo(zone) if zone else UTC).date()
    except (ValueError, KeyError, OSError):
        return now.astimezone(UTC).date()


async def today_of(
    factory: async_sessionmaker[AsyncSession], ctx: UserContext, now: datetime
) -> date:
    async with reading(factory, ctx) as r:
        zone = await r.home_zone()
    return local_today(now, zone)
