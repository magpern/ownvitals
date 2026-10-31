"""Reading a job's events for the SSE stream (ADR 0005 section 7): one short ``user_session`` per
read, none held open between reads.

Owner-scoped twice: every statement filters on ``owner_id = ctx.user_id`` and RLS answers for the
session's user. A foreign or unknown job is ``None``, the same answer (the route turns it into
404). The job's state is read **before** its events: a terminal state then implies its terminal
event is already committed and part of the events read next.

This module may open a ``user_session`` (``tests/session_scan.py``): web only, the owner's own
reads.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext
from ownvitals.db import user_session

_STATE = text("SELECT state FROM jobs WHERE id = :job_id AND owner_id = :owner_id")
_EVENTS = text(
    "SELECT seq, kind, payload FROM job_events "
    "WHERE job_id = :job_id AND owner_id = :owner_id AND seq > :after ORDER BY seq LIMIT :limit"
)


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq: int
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Page:
    state: str
    events: list[StoredEvent]


async def read_page(
    factory: async_sessionmaker[AsyncSession],
    ctx: UserContext,
    job_id: UUID,
    after: int,
    limit: int,
) -> Page | None:
    """The owner's job state and its events after ``after`` (at most ``limit``), or ``None``."""
    params = {"job_id": job_id, "owner_id": ctx.user_id}
    async with user_session(factory, ctx) as db:
        state = (await db.execute(_STATE, params)).scalar_one_or_none()
        if state is None:
            return None
        rows = (await db.execute(_EVENTS, {**params, "after": after, "limit": limit})).all()
    return Page(state, [StoredEvent(r.seq, r.kind, r.payload) for r in rows])
