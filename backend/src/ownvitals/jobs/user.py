"""The user's transitions on their own jobs (ADR 0005 section 2): create and cancel.

Both take the caller's ``user_session`` (``db``) and its ``UserContext``. Cancel locks the job
row (``SELECT ... FOR UPDATE``, RLS as the owner, plus ``owner_id = ctx.user_id``; another user's
id is not found, like an unknown one) and is idempotent by its target state, writing no second
event. User events carry ``lease_version`` 0.

``create`` first takes the owner's ``pg_advisory_xact_lock`` in the job-creation namespace (ADR
0005 section 9: ``(1, hashtext(owner_id::text))``), so two creations of one user are serialized
before either looks up its idempotency key or counts the per-user limit (``queued`` and
``running``). The lock is always taken before any job row lock, and no worker path takes it, so
it adds no lock cycle.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.context import UserContext, require_context
from ownvitals.db.queue_locks import JOB_CREATION
from ownvitals.jobs.events import notify_event, validated_payload

USER_LEASE_VERSION: Final = 0

_LOCK_OWNER = text("SELECT pg_advisory_xact_lock(:ns, hashtext(CAST(:owner_id AS text)))")
_ACTIVE = text(
    "SELECT count(*) FROM jobs WHERE owner_id = :owner_id AND state IN ('queued', 'running')"
)
_BY_KEY = text(
    "SELECT id, request_hash FROM jobs WHERE owner_id = :owner_id AND idempotency_key = :key"
)
_NEW_JOB = text(
    "INSERT INTO jobs (id, owner_id, kind, state, input, idempotency_key, request_hash, next_seq) "
    "VALUES (:id, :owner_id, :kind, 'queued', CAST(:input AS jsonb), :key, :request_hash, 1)"
)
_LOCK = text(
    "SELECT state, cancel_requested_at FROM jobs "
    "WHERE id = :job_id AND owner_id = :owner_id FOR UPDATE"
)
_CANCEL_NOW = text(
    "UPDATE jobs SET state = 'cancelled', cancel_requested_at = coalesce(cancel_requested_at, "
    "now()), finished_at = now(), next_seq = next_seq + 1, updated_at = now() "
    "WHERE id = :job_id AND owner_id = :owner_id AND state = 'queued' "
    "RETURNING next_seq"
)
_REQUEST_CANCEL = text(
    "UPDATE jobs SET cancel_requested_at = now(), next_seq = next_seq + 1, updated_at = now() "
    "WHERE id = :job_id AND owner_id = :owner_id AND state = 'running' "
    "AND cancel_requested_at IS NULL RETURNING next_seq"
)
_EVENT = text(
    "INSERT INTO job_events (id, owner_id, job_id, seq, kind, payload, lease_version) "
    "VALUES (:id, :owner_id, :job_id, :seq, :kind, CAST(:payload AS jsonb), :v)"
)
_NOTIFY_QUEUED = text("SELECT pg_notify('jobs_queued', '')")


async def lock_owner(db: AsyncSession, ctx: UserContext) -> None:
    """The owner's transaction-level advisory lock for job creation (ADR 0005 sections 2, 9)."""
    await db.execute(_LOCK_OWNER, {"ns": JOB_CREATION, "owner_id": str(ctx.user_id)})


async def active_jobs(db: AsyncSession, ctx: UserContext) -> int:
    """The owner's ``queued`` and ``running`` jobs: what the per-user limit counts."""
    return (await db.execute(_ACTIVE, {"owner_id": ctx.user_id})).scalar_one()


async def _event(db: AsyncSession, ctx: UserContext, job_id: UUID, seq: int, kind: str) -> None:
    await db.execute(
        _EVENT,
        {
            "id": uuid.uuid4(),
            "owner_id": ctx.user_id,
            "job_id": job_id,
            "seq": seq,
            "kind": kind,
            "payload": json.dumps(validated_payload(kind, {})),
            "v": USER_LEASE_VERSION,
        },
    )
    await notify_event(db, job_id)


@dataclass(frozen=True, slots=True)
class NewJob:
    """A validated ``POST /api/jobs``: the kind, the stored ``input`` JSON (canonical, validated
    by the kind's model), the browser's idempotency key and the SHA-256 of the canonical
    validated body."""

    kind: str
    input: str
    idempotency_key: str
    request_hash: bytes


@dataclass(frozen=True, slots=True)
class Created:
    job_id: UUID | None
    created: bool  # False: the same key and body again (the existing job), or refused
    reason: str | None = None  # idempotency_mismatch or busy
    limit: int | None = None  # with busy


async def create(db: AsyncSession, ctx: UserContext, new: NewJob, *, limit: int) -> Created:
    """``(new) -> queued`` in the caller's transaction, under the owner lock, in ADR 0005
    section 2's order: the idempotency key, then the per-user limit. A refusal writes nothing."""
    require_context(ctx)
    owner = {"owner_id": ctx.user_id}
    await lock_owner(db, ctx)
    found = (await db.execute(_BY_KEY, {**owner, "key": new.idempotency_key})).first()
    if found is not None:
        if bytes(found.request_hash) == new.request_hash:
            return Created(found.id, created=False)
        return Created(None, created=False, reason="idempotency_mismatch")
    if await active_jobs(db, ctx) >= limit:
        return Created(None, created=False, reason="busy", limit=limit)
    job_id = uuid.uuid4()
    await db.execute(
        _NEW_JOB,
        {
            **owner,
            "id": job_id,
            "kind": new.kind,
            "input": new.input,
            "key": new.idempotency_key,
            "request_hash": new.request_hash,
        },
    )
    await _event(db, ctx, job_id, 1, "queued")
    await db.execute(_NOTIFY_QUEUED)
    return Created(job_id, created=True)


async def cancel(db: AsyncSession, ctx: UserContext, job_id: UUID) -> str | None:
    """Cancel: a ``queued`` job ends ``cancelled`` at once; a ``running`` job gets
    ``cancel_requested_at`` and stops at its next checkpoint. Returns the job's state afterwards,
    or ``None`` if the user has no such job."""
    require_context(ctx)
    params = {"job_id": job_id, "owner_id": ctx.user_id}
    row = (await db.execute(_LOCK, params)).first()
    if row is None:
        return None
    if row.state == "queued":
        seq = (await db.execute(_CANCEL_NOW, params)).scalar_one()
        await _event(db, ctx, job_id, seq, "cancelled")
        return "cancelled"
    if row.state == "running" and row.cancel_requested_at is None:
        seq = (await db.execute(_REQUEST_CANCEL, params)).scalar_one()
        await _event(db, ctx, job_id, seq, "cancel_requested")
    return row.state
