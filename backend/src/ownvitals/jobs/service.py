"""The jobs API's transactions (ADR 0005 section 2): one ``user_session`` per request, as the
signed-in user, around the transitions of ``ownvitals.jobs.user``.

This is the web side of ``ownvitals.jobs`` and never runs in the worker; it may open a
``user_session`` (``tests/session_scan.py``) because these are the user's own writes, not a
worker's, so the fence does not apply. Every read and write filters on ``owner_id =
ctx.user_id`` under RLS: another user's job id is not found, exactly like an unknown one.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext, require_context
from ownvitals.db import user_session
from ownvitals.jobs import user

Factory = async_sessionmaker[AsyncSession]

KEY_UNIQUE = "jobs_owner_id_idempotency_key_key"
INPUT_SIZE = "jobs_input"

_VIEW = text(
    "SELECT id, kind, state, cancel_requested_at IS NOT NULL AS cancel_requested, "
    "error_category, attempt, max_attempts, created_at, updated_at, finished_at "
    "FROM jobs WHERE id = :job_id AND owner_id = :owner_id"
)


@dataclass(frozen=True, slots=True)
class JobView:
    """What the owner sees of a job: no input, key, hash, lease, token or worker name."""

    id: UUID
    kind: str
    state: str
    cancel_requested: bool
    error_category: str | None
    attempt: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


async def _view(db: AsyncSession, ctx: UserContext, job_id: UUID) -> JobView | None:
    row = (await db.execute(_VIEW, {"job_id": job_id, "owner_id": ctx.user_id})).first()
    return None if row is None else JobView(*row)


def _constraint(error: IntegrityError) -> str | None:
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None)


@dataclass(frozen=True, slots=True)
class CreateResult:
    created: bool
    job: JobView | None
    reason: str | None = None  # see ``user.Created``, plus too_large
    limit: int | None = None


async def create(
    factory: Factory, ctx: UserContext, new: user.NewJob, *, limit: int
) -> CreateResult:
    require_context(ctx)
    for retry in (False, True):
        try:
            async with user_session(factory, ctx) as db:
                done = await user.create(db, ctx, new, limit=limit)
                if done.job_id is None:
                    return CreateResult(False, None, done.reason, done.limit)
                return CreateResult(done.created, await _view(db, ctx, done.job_id))
        except IntegrityError as error:
            # The owner lock serializes creations, so these are backstops, not the normal path.
            name = _constraint(error)
            if name == INPUT_SIZE:
                return CreateResult(False, None, "too_large")
            if name != KEY_UNIQUE or retry:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


async def get(factory: Factory, ctx: UserContext, job_id: UUID) -> JobView | None:
    async with user_session(factory, ctx) as db:
        return await _view(db, ctx, job_id)


async def cancel(factory: Factory, ctx: UserContext, job_id: UUID) -> JobView | None:
    async with user_session(factory, ctx) as db:
        if await user.cancel(db, ctx, job_id) is None:
            return None
        return await _view(db, ctx, job_id)
