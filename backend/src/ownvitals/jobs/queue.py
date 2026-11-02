"""Calls of the queue's ``SECURITY DEFINER`` functions (ADR 0005 section 3), the only cross-user
paths into the job tables. They run in a ``system_session`` (no ``app.user_id``) and return only
``(job_id, owner_id, lease_version)`` rows or counts; all times are the database's ``now()``.

This module may open a ``system_session`` (``tests/session_scan.py``): the definer functions are
the maintenance context of ADR 0002 section 4, and the session reaches no owned row itself.
"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
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
_ENQUEUE = text(
    "SELECT public.queue_enqueue(:connection_id, :kind, :origin, CAST(:payload AS jsonb), "
    ":singleton_key, :max_attempts, :target_secret_id)"
)
_CANCEL_SECRET = text("SELECT public.queue_cancel_for_secret(:kind, :secret_id)")
_REPLAY = text("SELECT job_id, owner_id FROM public.queue_replay(:job_id)")


class ReplayRefused(Exception):
    """``queue_replay`` refused the id: it is not a dead-lettered job, or a singleton is already
    queued. ``reason`` is ``not_replayable`` or ``already_queued``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Replayed:
    """The new queued job and the owner it belongs to."""

    __slots__ = ("job_id", "owner_id")

    def __init__(self, job_id: UUID, owner_id: UUID) -> None:
        self.job_id = job_id
        self.owner_id = owner_id


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


def _refused(exc: DBAPIError) -> str | None:
    message = str(exc)
    if "not replayable" in message:
        return "not_replayable"
    if "already queued" in message:
        return "already_queued"
    return None


async def enqueue(
    factory: Factory,
    *,
    connection_id: UUID,
    kind: str,
    origin: str,
    payload: str,
    singleton_key: str | None,
    max_attempts: int,
    target_secret_id: UUID | None,
) -> UUID:
    """``queue_enqueue``: one webhook or scheduler job, owned by the connection's owner."""
    async with system_session(factory) as db:
        job_id = (
            await db.execute(
                _ENQUEUE,
                {
                    "connection_id": connection_id,
                    "kind": kind,
                    "origin": origin,
                    "payload": payload,
                    "singleton_key": singleton_key,
                    "max_attempts": max_attempts,
                    "target_secret_id": target_secret_id,
                },
            )
        ).scalar_one()
    return job_id


async def cancel_for_secret(factory: Factory, kind: str, secret_id: UUID) -> int:
    """``queue_cancel_for_secret``: queued jobs of ``kind`` for that webhook secret, any owner."""
    async with system_session(factory) as db:
        count = await db.execute(_CANCEL_SECRET, {"kind": kind, "secret_id": secret_id})
        return count.scalar_one()


async def replay(factory: Factory, job_id: UUID) -> Replayed:
    """``queue_replay``: a new queued job. The dead-lettered row is left as it is."""
    try:
        async with system_session(factory) as db:
            row = (await db.execute(_REPLAY, {"job_id": job_id})).one()
    except DBAPIError as exc:
        reason = _refused(exc)
        if reason is None:
            raise
        raise ReplayRefused(reason) from None
    return Replayed(row.job_id, row.owner_id)


async def stats(factory: Factory) -> list[tuple[str, int]]:
    """``queue_stats()``: counts per job state, across all users and without ids (``python -m
    ownvitals jobs stats``; the admin area of #12)."""
    async with system_session(factory) as db:
        rows = (await db.execute(_STATS)).all()
    return [(str(r.metric), int(r.total)) for r in rows]
