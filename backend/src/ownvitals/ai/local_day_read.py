"""Reads the user's zone once (``views.reads``, forced RLS, the caller's own rows) and applies
``local_day.local_today``. Only a date leaves this module (issue #443)."""

from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai.local_day import local_today
from ownvitals.context import UserContext
from ownvitals.views.reads import reading


async def today_of(
    factory: async_sessionmaker[AsyncSession], ctx: UserContext, now: datetime
) -> date:
    async with reading(factory, ctx) as r:
        zone = await r.home_zone(now)
    return local_today(now, zone)
