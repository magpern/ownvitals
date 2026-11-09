"""The AI audit log's retention (ADR 0014 section 6; #331).

``ai_audit`` rows are kept ``ai_audit_retention_months`` (12 by default, never fewer) and then
deleted by the worker's hourly purge tick (``ownvitals.worker.upkeep.purge``), whether or not
``ai_insights`` is still on: retention applies to rows written while it was. Account deletion
removes a user's rows sooner (``owner_id`` cascade); withdrawal keeps them (ADR 0014 section 3).

The app role may not delete ``ai_audit`` rows (the log is append-only for it), so the purge is
the definer function ``ai_audit_purge(p_months, p_limit)`` of ``docker/postgres/ai/50-ai.sql``:
the cross-user maintenance path of ADR 0002 section 4, in a session with no user, returning a
count only. The function refuses fewer than 12 months, so not even a caller with the app role can
shorten the retention. When 50-ai.sql is not installed nothing is called: no ``ai_audit`` row can
exist then, since every model call first reserves through ``ai_reserve``.
"""

from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.db import system_session

MIN_MONTHS: Final = 12
MAX_MONTHS: Final = 120
AUDIT_PURGE_LIMIT: Final = 1000
AUDIT_PURGE_BATCHES: Final = 10

_INSTALLED = text(
    "SELECT pg_catalog.to_regprocedure('public.ai_audit_purge(integer, integer)') IS NOT NULL"
)
_PURGE = text("SELECT public.ai_audit_purge(:months, :n)")


def _bounded(name: str, value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


async def purge_expired_audit(
    factory: async_sessionmaker[AsyncSession],
    months: int,
    *,
    limit: int = AUDIT_PURGE_LIMIT,
    batches: int = AUDIT_PURGE_BATCHES,
) -> int:
    """Delete the audit rows older than ``months`` months, at most ``limit`` per transaction and
    ``batches`` transactions per tick (the rest at the next tick). Returns how many went."""
    months = _bounded("months", months, MIN_MONTHS, MAX_MONTHS)
    limit = _bounded("limit", limit, 1, 10_000)
    batches = _bounded("batches", batches, 1, 100)
    total = 0
    for _ in range(batches):
        async with system_session(factory) as db:
            if not (await db.execute(_INSTALLED)).scalar_one():
                return total
            deleted = int((await db.execute(_PURGE, {"months": months, "n": limit})).scalar_one())
        total += deleted
        if deleted < limit:
            break
    return total
