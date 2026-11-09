"""The export's cross-user purge (ADR 0013 section 4; #333).

An export whose owner never comes back must not stay in ``export_chunks`` (and in the nightly
backups) until they do. The worker's hourly purge tick (``ownvitals.worker.upkeep.purge``) calls
``purge_expired_exports``, which calls the definer function ``exports_purge_expired()`` of
``docker/postgres/exports/60-exports.sql`` in a session with no user: the maintenance context of
ADR 0002 section 4, the only cross-user export path. The function takes no argument and returns
four counts, nothing else. For every owner it

- marks ready exports past ``expires_at`` expired, with an ``export_expired`` audit event each;
- marks building exports whose job has ended without an archive failed (``build_failed``), with
  an ``export_failed`` event each, as the owner's own tidy does (``export_store.tidy``);
- deletes the chunks of failed and expired exports;
- deletes failed and expired rows older than 90 days.

Each call does a bounded amount (100 exports per step, 1000 chunks); the tick calls again while
a call did something, at most ``PURGE_BATCHES`` times, and the rest waits for the next tick.
When 60-exports.sql is not installed nothing is called and the owner's tidy stays the only path.
Logs (``upkeep.quietly``) carry the total only, never an export id.
"""

from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.db import system_session

PURGE_BATCHES: Final = 10

_INSTALLED = text("SELECT pg_catalog.to_regprocedure('public.exports_purge_expired()') IS NOT NULL")
_PURGE = text("SELECT expired, failed, chunks, deleted FROM public.exports_purge_expired()")


@dataclass(frozen=True, slots=True)
class Purged:
    """The counts of one call: exports expired and failed now, chunks and rows deleted."""

    expired: int = 0
    failed: int = 0
    chunks: int = 0
    deleted: int = 0

    @property
    def total(self) -> int:
        return self.expired + self.failed + self.chunks + self.deleted


async def purge_once(factory: async_sessionmaker[AsyncSession]) -> Purged | None:
    """One call of ``exports_purge_expired()`` in its own transaction; ``None`` when the
    function is not installed."""
    async with system_session(factory) as db:
        if not (await db.execute(_INSTALLED)).scalar_one():
            return None
        row = (await db.execute(_PURGE)).one()
    return Purged(
        expired=int(row.expired),
        failed=int(row.failed),
        chunks=int(row.chunks),
        deleted=int(row.deleted),
    )


async def purge_expired_exports(
    factory: async_sessionmaker[AsyncSession], *, batches: int = PURGE_BATCHES
) -> int:
    """Calls while a call changed something, at most ``batches`` times. Returns the total of
    the counts."""
    if type(batches) is not int or not 1 <= batches <= 100:
        raise ValueError("batches must be an integer from 1 to 100")
    total = 0
    for _ in range(batches):
        purged = await purge_once(factory)
        if purged is None or purged.total == 0:
            break
        total += purged.total
    return total
