"""The web side of the export (ADR 0013 section 4; #211): the user's own export rows, in the
signed-in user's ``user_session`` (forced RLS; every statement also names ``owner_id``).

- ``request``: under the owner's job-creation lock (ADR 0005 section 9, the lock
  ``jobs.user.create`` takes too), tidy the user's rows (``tidy``), refuse a second build in
  progress (``export_in_progress``) or a full job slot (``busy``), take one slot of the 24-hour
  limit, then insert the export row and its ``export_build`` job in one transaction.
- ``tidy``: a ``building`` row whose job has ended without making it ready becomes ``failed``
  (``build_failed``); a ``ready`` row past ``expires_at`` becomes ``expired``; the chunks of both
  are deleted; ``failed`` and ``expired`` rows older than 90 days are deleted. It returns the ids
  that expired now, for the audit. Downloads refuse an expired row whether or not this ran.
- ``begin_download``: locks the row, refuses what is not ``ready``, past its expiry or out of
  attempts (at most 3), and counts the attempt before any byte is sent.
- ``read_chunk`` and ``finish_download``: the archive in order, then, after the last byte, the
  row ``expired`` with ``download_count`` 1 and its chunks deleted ("single use" is one completed
  transfer).

The cross-user purge of abandoned rows (``exports_purge_expired()``, a ``SECURITY DEFINER``
function) is not built yet: until then an expired archive's chunks are removed the next time
its owner opens the export list, requests or downloads (``docs/dev/reports.md``, known gaps).
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext, require_context
from ownvitals.db import user_session
from ownvitals.jobs import user as user_jobs
from ownvitals.reports.tables import MAX_DOWNLOAD_ATTEMPTS

Factory = async_sessionmaker[AsyncSession]
KIND: Final = "export_build"  # ownvitals.reports.export.KIND (web never imports the builder)
LIST_LIMIT: Final = 10
ONE_BUILDING: Final = "exports_one_building"

_FAIL_ENDED = text(
    "UPDATE exports AS e SET state = 'failed', failure = 'build_failed' FROM jobs AS j "
    "WHERE e.owner_id = :owner AND e.state = 'building' AND j.owner_id = :owner "
    "AND j.id = e.job_id AND j.state IN ('completed', 'failed', 'dead_letter', 'cancelled')"
)
_EXPIRE = text(
    "UPDATE exports SET state = 'expired' WHERE owner_id = :owner AND state = 'ready' "
    "AND expires_at <= now() RETURNING id"
)
_DROP_CHUNKS = text(
    "DELETE FROM export_chunks AS c USING exports AS e WHERE c.owner_id = :owner "
    "AND e.owner_id = :owner AND e.id = c.export_id AND e.state IN ('failed', 'expired')"
)
_DROP_OLD = text(
    "DELETE FROM exports WHERE owner_id = :owner AND state IN ('failed', 'expired') "
    "AND created_at < now() - interval '90 days'"
)
_BUILDING = text(
    "SELECT count(*) FROM exports WHERE owner_id = :owner AND kind = 'data' AND state = 'building'"
)
_INSERT = text(
    "INSERT INTO exports (id, owner_id, kind, state, job_id) "
    "VALUES (:id, :owner, 'data', 'building', :job)"
)
_LIST = text(
    "SELECT id, state, created_at, ready_at, expires_at, size, failure, largest, "
    "download_attempts FROM exports WHERE owner_id = :owner AND kind = 'data' "
    "ORDER BY created_at DESC, id LIMIT :limit"
)
_LOCK = text(
    "SELECT state, expires_at > now() AS live, download_attempts, size FROM exports "
    "WHERE id = :id AND owner_id = :owner AND kind = 'data' FOR UPDATE"
)
_ATTEMPT = text(
    "UPDATE exports SET download_attempts = download_attempts + 1 "
    "WHERE id = :id AND owner_id = :owner RETURNING download_attempts"
)
_EXPIRE_ONE = text(
    "UPDATE exports SET state = 'expired' WHERE id = :id AND owner_id = :owner AND state = 'ready'"
)
_CHUNKS = text("SELECT count(*) FROM export_chunks WHERE export_id = :id AND owner_id = :owner")
_CHUNK = text(
    "SELECT bytes FROM export_chunks WHERE export_id = :id AND owner_id = :owner AND seq = :seq"
)
_DONE = text(
    "UPDATE exports SET state = 'expired', download_count = 1 WHERE id = :id "
    "AND owner_id = :owner AND state = 'ready' RETURNING id"
)
_DELETE_CHUNKS = text("DELETE FROM export_chunks WHERE export_id = :id AND owner_id = :owner")


class Refused(Exception):
    """A request or download that is refused: ``reason`` is a response code."""

    def __init__(self, reason: str, *, expired: tuple[UUID, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.expired = expired


@dataclass(frozen=True, slots=True)
class ExportView:
    """What the owner sees of one export: no content, hash, job or chunk."""

    id: UUID
    state: str
    created_at: datetime
    ready_at: datetime | None
    expires_at: datetime | None
    size: int | None
    failure: str | None
    largest: str | None
    attempts_left: int


@dataclass(frozen=True, slots=True)
class Requested:
    export_id: UUID
    expired: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class Download:
    size: int
    attempt: int
    chunks: int


async def tidy(db: AsyncSession, ctx: UserContext) -> tuple[UUID, ...]:
    owner = {"owner": ctx.user_id}
    await db.execute(_FAIL_ENDED, owner)
    expired = tuple((await db.execute(_EXPIRE, owner)).scalars())
    await db.execute(_DROP_CHUNKS, owner)
    await db.execute(_DROP_OLD, owner)
    return expired


async def request(
    factory: Factory,
    ctx: UserContext,
    *,
    job_limit: int,
    reserve: Callable[[], Awaitable[None]],
) -> Requested:
    """A new export and its build job, or ``Refused``. ``reserve`` takes the 24-hour slot (it
    raises above the limit); it runs only once nothing else refuses."""
    require_context(ctx)
    export_id = uuid.uuid4()
    body = json.dumps({"export_id": str(export_id)}, sort_keys=True, separators=(",", ":"))
    new = user_jobs.NewJob(
        kind=KIND,
        input=body,
        idempotency_key=f"export-{export_id.hex}",
        request_hash=hashlib.sha256(body.encode()).digest(),
    )
    refusal: str | None = None
    try:
        async with user_session(factory, ctx) as db:
            await user_jobs.lock_owner(db, ctx)
            expired = await tidy(db, ctx)  # committed with a refusal too
            if (await db.execute(_BUILDING, {"owner": ctx.user_id})).scalar_one():
                refusal = "export_in_progress"
            elif await user_jobs.active_jobs(db, ctx) >= job_limit:
                refusal = "busy"
            else:
                await reserve()
                created = await user_jobs.create(db, ctx, new, limit=job_limit)
                if created.job_id is None:  # the owner lock makes this unreachable
                    raise Refused("busy")
                params = {"id": export_id, "owner": ctx.user_id, "job": created.job_id}
                await db.execute(_INSERT, params)
    except IntegrityError as error:
        name = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
        if name == ONE_BUILDING:
            raise Refused("export_in_progress") from None
        raise
    if refusal is not None:
        raise Refused(refusal, expired=expired)
    return Requested(export_id, expired)


async def list_exports(
    factory: Factory, ctx: UserContext
) -> tuple[list[ExportView], tuple[UUID, ...]]:
    """The user's latest exports, newest first, after ``tidy``; and the ids that expired now."""
    async with user_session(factory, ctx) as db:
        expired = await tidy(db, ctx)
        rows = (await db.execute(_LIST, {"owner": ctx.user_id, "limit": LIST_LIMIT})).all()
    views = [
        ExportView(
            id=r.id,
            state=r.state,
            created_at=r.created_at,
            ready_at=r.ready_at,
            expires_at=r.expires_at,
            size=r.size,
            failure=r.failure,
            largest=r.largest,
            attempts_left=max(0, MAX_DOWNLOAD_ATTEMPTS - r.download_attempts),
        )
        for r in rows
    ]
    return views, expired


async def begin_download(factory: Factory, ctx: UserContext, export_id: UUID) -> Download:
    """Count one download attempt of a ready, live export, or raise ``Refused``."""
    params = {"id": export_id, "owner": ctx.user_id}
    async with user_session(factory, ctx) as db:
        row = (await db.execute(_LOCK, params)).first()
        if row is None:
            raise Refused("not_found")
        if row.state == "ready" and not row.live:
            await db.execute(_EXPIRE_ONE, params)
            await db.execute(_DELETE_CHUNKS, params)
            refusal = Refused("export_expired", expired=(export_id,))
        elif row.state in ("expired", "failed"):
            refusal = Refused("export_expired")
        elif row.state != "ready":
            refusal = Refused("export_not_ready")
        elif row.download_attempts >= MAX_DOWNLOAD_ATTEMPTS:
            refusal = Refused("export_download_spent")
        else:
            attempt = (await db.execute(_ATTEMPT, params)).scalar_one()
            chunks = (await db.execute(_CHUNKS, params)).scalar_one()
            return Download(size=int(row.size), attempt=int(attempt), chunks=int(chunks))
    raise refusal


async def read_chunk(factory: Factory, ctx: UserContext, export_id: UUID, seq: int) -> bytes:
    """One chunk, in its own transaction; ``LookupError`` when it is gone (expired meanwhile)."""
    params = {"id": export_id, "owner": ctx.user_id, "seq": seq}
    async with user_session(factory, ctx) as db:
        data = (await db.execute(_CHUNK, params)).scalar_one_or_none()
    if data is None:
        raise LookupError("chunk gone")
    return bytes(data)


async def finish_download(factory: Factory, ctx: UserContext, export_id: UUID) -> bool:
    """After the last byte: the export ``expired`` with one completed download, chunks gone."""
    params = {"id": export_id, "owner": ctx.user_id}
    async with user_session(factory, ctx) as db:
        done = (await db.execute(_DONE, params)).first() is not None
        await db.execute(_DELETE_CHUNKS, params)
    return done
