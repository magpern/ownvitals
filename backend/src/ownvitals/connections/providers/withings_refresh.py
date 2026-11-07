"""The token refresh's writes on a Withings ``connections`` row (ADR 0004 sections 6 and 7;
``withings.py`` re-exports them).

Token writes compare-and-swap ``token_generation``. The refresh lock is session-scoped on its
own pooled connection, so the intent commits before the Withings call and the lock still
covers that call. With a lease every write is fenced and ends before any Withings call.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.providers.withings_payload import (
    _C,
    _REFRESH_TTL,
    PROVIDER,
    Factory,
    _as_public,
    _payload,
    _token,
)
from ownvitals.connections.repository import write_tokens
from ownvitals.connections.tables import connections
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import connection_aad, connection_wrap_aad, seal
from ownvitals.crypto.keks import KeyRing
from ownvitals.db import user_session
from ownvitals.db.queue_locks import TOKEN_REFRESH
from ownvitals.jobs.fence import Lease, fenced_session
from ownvitals.notify.outbox import queue_owner_notice

# The worker client's total deadline (``egress.WITHINGS_TOTAL_SECONDS``). The lease is extended
# by this plus the fence's own margin, in the same transaction as the intent.
_CALL_SECONDS = 30.0


_LOCK = text("SELECT pg_advisory_lock(:ns, hashtext(CAST(:connection_id AS text)))")
_UNLOCK = text("SELECT pg_advisory_unlock(:ns, hashtext(CAST(:connection_id AS text)))")


def _engine(factory: Factory) -> AsyncEngine:
    bind = factory.kw.get("bind")
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("the session factory is not bound to an engine")
    return bind


def _actor(ctx: UserContext, lease: Lease | None) -> UserContext:
    return ctx if lease is None else lease.ctx


@asynccontextmanager
async def _writes(
    factory: Factory, ctx: UserContext, lease: Lease | None
) -> AsyncIterator[tuple[AsyncSession, Any]]:
    """A short transaction. With a lease it is fenced, and it ends before any Withings call."""
    actor = _actor(ctx, lease)
    if lease is None:
        async with user_session(factory, actor) as db:
            yield db, None
    else:
        async with fenced_session(factory, lease) as fenced:
            yield fenced.db, fenced


@asynccontextmanager
async def hold_refresh_lock(factory: Factory, connection_id: UUID) -> AsyncIterator[None]:
    """Session lock on a second connection, held across the intent commit and the call."""
    async with _engine(factory).connect() as conn:
        await conn.execute(_LOCK, {"ns": TOKEN_REFRESH, "connection_id": str(connection_id)})
        await conn.commit()
        try:
            yield
        finally:
            if conn.in_transaction():
                await conn.rollback()
            await conn.execute(_UNLOCK, {"ns": TOKEN_REFRESH, "connection_id": str(connection_id)})
            await conn.commit()


async def mark_intent(
    factory: Factory,
    ctx: UserContext,
    connection_id: UUID,
    generation: int,
    lease: Lease | None = None,
) -> str:
    """Commit ``refresh_in_flight = generation``. ``cancelled`` when the job asked to stop."""
    async with _writes(factory, ctx, lease) as (db, fenced):
        if fenced is not None and await fenced.extend_lease_for_call(_CALL_SECONDS):
            return "cancelled"
        marked = (
            await db.execute(
                update(connections)
                .where(
                    _C.id == connection_id,
                    _C.owner_id == _actor(ctx, lease).user_id,
                    _C.token_generation == generation,
                    _C.refresh_in_flight.is_(None),
                    _C.status == "connected",
                )
                .values(
                    refresh_in_flight=generation,
                    refresh_intent_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(_C.id)
            )
        ).first()
    return "marked" if marked is not None else "absent"


async def clear_intent(
    factory: Factory,
    ctx: UserContext,
    connection_id: UUID,
    generation: int,
    lease: Lease | None = None,
) -> bool:
    """Drop the intent when Withings did not rotate the token. The generation stays."""
    async with _writes(factory, ctx, lease) as (db, _fenced):
        cleared = (
            await db.execute(
                update(connections)
                .where(
                    _C.id == connection_id,
                    _C.owner_id == _actor(ctx, lease).user_id,
                    _C.token_generation == generation,
                    _C.refresh_in_flight == generation,
                )
                .values(refresh_in_flight=None, refresh_intent_at=None, updated_at=func.now())
                .returning(_C.id)
            )
        ).first()
    return cleared is not None


async def give_up(
    factory: Factory,
    ctx: UserContext,
    connection_id: UUID,
    generation: int,
    lease: Lease | None = None,
) -> bool:
    """``needs_reconnect`` and the in-app notice, plus the owner notice in the mail outbox, in
    the same transaction (PLAN section 4.3 step 4, ADR 0007 section 4). Credentials stay. The
    worker opens no SMTP connection: web sends the notice."""
    async with _writes(factory, ctx, lease) as (db, _fenced):
        given = (
            await db.execute(
                update(connections)
                .where(
                    _C.id == connection_id,
                    _C.owner_id == _actor(ctx, lease).user_id,
                    _C.token_generation == generation,
                )
                .values(
                    status="needs_reconnect",
                    last_error="needs_reconnect",
                    refresh_in_flight=None,
                    refresh_intent_at=None,
                    updated_at=func.now(),
                )
                .returning(_C.id)
            )
        ).first()
        if given is not None:
            await queue_owner_notice(db, "connection_needs_reconnect")
    return given is not None


async def commit_refresh(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    connection_id: UUID,
    generation: int,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    lease: Lease | None = None,
) -> bool:
    """CAS-write generation + 1 and clear the intent, or change nothing."""
    if type(expires_in) is not int or not 1 <= expires_in <= 86_400:
        raise ConnectionFailure("unknown")
    access, refresh = _token(access_token), _token(refresh_token)
    actor = _actor(ctx, lease)
    access_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    async with _writes(factory, ctx, lease) as (db, _fenced):
        kek = await ring.current(db)
        row = (
            await db.execute(
                select(_C.public).where(
                    _C.id == connection_id,
                    _C.owner_id == actor.user_id,
                    _C.token_generation == generation,
                )
            )
        ).first()
        if row is None:
            return False
        sealed = seal(
            _payload(access, refresh, access_expires_at),
            connection_aad(actor.user_id, PROVIDER, connection_id),
            kek,
            connection_wrap_aad(kek.key_id, actor.user_id, PROVIDER, connection_id),
        )
        return await write_tokens(
            db,
            actor,
            connection_id,
            generation,
            sealed,
            public=_as_public(row.public),
            expires_at=datetime.now(UTC) + _REFRESH_TTL,
        )
