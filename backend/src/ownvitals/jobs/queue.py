"""Calls of the queue's ``SECURITY DEFINER`` functions (ADR 0005 section 3), the only cross-user
paths into the job tables. They run in a ``system_session`` (no ``app.user_id``) and return only
``(job_id, owner_id, lease_version)`` rows or counts; all times are the database's ``now()``.

This module may open a ``system_session`` (``tests/session_scan.py``): the definer functions are
the maintenance context of ADR 0002 section 4, and the session reaches no owned row itself.
"""

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.db import system_session
from ownvitals.jobs.issuer import Claimed

Factory = async_sessionmaker[AsyncSession]

REAP_LIMIT = 100  # queue_reap's maximum per call

_CLAIM = text(
    "SELECT job_id, owner_id, lease_version "
    "FROM public.queue_claim(:worker, :lease, CAST(:kinds AS text[]))"
)
_REAP = text("SELECT public.queue_reap(:n)")
_STATS = text("SELECT metric, total FROM public.queue_stats()")


async def claim(
    factory: Factory, worker: str, lease_seconds: int, kinds: Sequence[str]
) -> Claimed | None:
    if not kinds:
        return None
    async with system_session(factory) as db:
        row = (
            await db.execute(
                _CLAIM, {"worker": worker, "lease": lease_seconds, "kinds": list(kinds)}
            )
        ).first()
    return None if row is None else Claimed(row.job_id, row.owner_id, row.lease_version)


async def reap(factory: Factory, limit: int = REAP_LIMIT) -> int:
    async with system_session(factory) as db:
        return (await db.execute(_REAP, {"n": limit})).scalar_one()


async def stats(factory: Factory) -> list[tuple[str, int]]:
    """``queue_stats()``: counts per job state, across all users and without ids (``python -m
    ownvitals jobs stats``; the admin area of #12)."""
    async with system_session(factory) as db:
        rows = (await db.execute(_STATS)).all()
    return [(str(r.metric), int(r.total)) for r in rows]
