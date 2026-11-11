"""The AI conversation retention (ADR 0014 section 5; #395).

A conversation last updated more than ``ai_conversation_retention_days`` days ago (90 by
default, never more, never fewer than 30) is deleted with its messages by the worker's hourly
purge tick (``ownvitals.worker.upkeep.purge``), whether or not ``ai_insights`` is still on:
retention applies to the history written while it was. Withdrawal and account deletion remove a
user's conversations sooner (ADR 0014 section 3).

The purge reaches every user's conversations, so it is the definer function
``ai_purge_expired(p_days, p_limit)`` of ``docker/postgres/ai/50-ai.sql``: the cross-user
maintenance path of ADR 0002 section 4, in a session with no user, returning a count only. The
function refuses a retention outside 30 to 90 days, so no caller with the app role can keep
history longer or wipe every user's recent history. When 50-ai.sql is not installed nothing is
called (no assistant reply can be reserved then; db-functions installs the script with the rest,
and the startup check reports the family as absent).
"""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.db import system_session

MIN_DAYS: Final = 30
MAX_DAYS: Final = 90
CONVERSATION_PURGE_LIMIT: Final = 500
CONVERSATION_PURGE_BATCHES: Final = 10

_INSTALLED = text(
    "SELECT pg_catalog.to_regprocedure('public.ai_purge_expired(integer, integer)') IS NOT NULL"
)
_PURGE = text("SELECT public.ai_purge_expired(:days, :n)")


def _bounded(name: str, value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


async def purge_expired_conversations(
    factory: async_sessionmaker[AsyncSession],
    days: int,
    *,
    limit: int = CONVERSATION_PURGE_LIMIT,
    batches: int = CONVERSATION_PURGE_BATCHES,
) -> int:
    """Delete the conversations last updated more than ``days`` days ago, at most ``limit`` per
    transaction and ``batches`` transactions per tick (the rest at the next tick). Returns how
    many conversations went (their messages go with them)."""
    days = _bounded("days", days, MIN_DAYS, MAX_DAYS)
    limit = _bounded("limit", limit, 1, 10_000)
    batches = _bounded("batches", batches, 1, 100)
    total = 0
    for _ in range(batches):
        async with system_session(factory) as db:
            if not (await db.execute(_INSTALLED)).scalar_one():
                return total
            deleted = int((await db.execute(_PURGE, {"days": days, "n": limit})).scalar_one())
        total += deleted
        if deleted < limit:
            break
    return total
