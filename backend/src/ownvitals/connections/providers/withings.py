"""Withings rows on ``connections`` (ADR 0004 sections 6 and 7).

Token writes compare-and-swap ``token_generation``. The refresh lock is session-scoped on its
own pooled connection, so the intent commits before the Withings call and the lock still
covers that call. The app client secret lives in ``app_secret``.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.repository import write_tokens
from ownvitals.connections.tables import DISCONNECTED, connections
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import (
    DecryptFailed,
    canonical_json,
    connection_aad,
    connection_wrap_aad,
    open_sealed,
    seal,
)
from ownvitals.crypto.keks import KeyRing, KeyUnavailable
from ownvitals.db import user_session
from ownvitals.db.queue_locks import TOKEN_REFRESH
from ownvitals.jobs.fence import Lease, fenced_session
from ownvitals.notify.outbox import queue_owner_notice

PROVIDER = "withings"
_REFRESH_TTL = timedelta(days=365)
# The worker client's total deadline (``egress.WITHINGS_TOTAL_SECONDS``). The lease is extended
# by this plus the fence's own margin, in the same transaction as the intent.
_CALL_SECONDS = 30.0
_TOKEN_MIN = 8
_TOKEN_MAX = 256
_C = connections.c
_LOCK = text("SELECT pg_advisory_lock(:ns, hashtext(CAST(:connection_id AS text)))")
_UNLOCK = text("SELECT pg_advisory_unlock(:ns, hashtext(CAST(:connection_id AS text)))")
_REASONS = frozenset({"linked_elsewhere", "other_withings_account", "conflict"})

Factory = async_sessionmaker[AsyncSession]


class GrantRefused(Exception):
    """The Withings account was not stored. ``reason`` is a fixed code, with no account id."""

    def __init__(self, reason: str) -> None:
        if reason not in _REASONS:
            raise ValueError("unknown grant refusal")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StoredGrant:
    """The row that now holds the grant. No token."""

    id: UUID
    generation: int
    status: str


@dataclass(frozen=True, slots=True, repr=False)
class OpenWithings:
    """A decrypted Withings connection. ``repr`` has no token and no Withings user id."""

    id: UUID
    generation: int
    status: str
    access_token: str
    refresh_token: str
    public: dict[str, Any]
    intent: int | None
    stale_intent: bool

    def __repr__(self) -> str:
        return f"OpenWithings(id={self.id}, generation={self.generation}, status={self.status!r})"


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


def _scope_list(scope: str) -> list[str]:
    items = [part for part in scope.split(",") if part]
    if len(items) > 20 or any(
        len(part) > 40 or not part.replace(".", "").isalpha() for part in items
    ):
        raise ConnectionFailure("unknown")
    return items


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN_MIN <= len(value) <= _TOKEN_MAX:
        raise ConnectionFailure("unknown")
    return value


def _payload(access: str, refresh: str, access_expires_at: datetime) -> bytes:
    return canonical_json(
        {
            "access_expires_at": access_expires_at.isoformat(),
            "access_token": access,
            "refresh_token": refresh,
        }
    )


def _public(app_id: int, userid: int, scopes: list[str]) -> dict[str, Any]:
    return {"app_id": app_id, "withings_userid": userid, "scopes": scopes}


def _as_public(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ConnectionFailure("decrypt_failed")
    return value


def _matches(public: dict[str, Any], app_id: int, userid: int) -> bool:
    app = public.get("app_id")
    user = public.get("withings_userid")
    if app is None or user is None:
        return False
    try:
        return int(app) == app_id and int(user) == userid
    except (TypeError, ValueError):
        return False


async def open_withings(
    factory: Factory, ring: KeyRing, ctx: UserContext, connection_id: UUID
) -> OpenWithings:
    """Decrypt one Withings row. A GCM failure sets ``needs_reconnect``."""
    failure: str | None = None
    opened: OpenWithings | None = None
    async with user_session(factory, ctx) as db:
        row = (
            await db.execute(
                select(
                    _C.id,
                    _C.status,
                    _C.token_generation,
                    _C.refresh_in_flight,
                    _C.public,
                    _C.secret,
                    _C.secret_nonce,
                    _C.wrapped_dek,
                    _C.dek_nonce,
                    _C.key_id,
                    literal_column(
                        "(refresh_intent_at IS NOT NULL AND refresh_intent_at < "
                        "now() - interval '8 hours')"
                    ).label("stale"),
                ).where(_C.id == connection_id, _C.owner_id == ctx.user_id)
            )
        ).first()
        if row is None or row.status != "connected" or row.secret is None or row.key_id is None:
            failure = "not_connected"
        else:
            try:
                kek = await ring.kek_for(db, row.key_id)
            except KeyUnavailable:
                failure = "key_unavailable"
                kek = None
            payload: bytes | None = None
            if kek is not None:
                try:
                    payload = open_sealed(
                        row.secret,
                        row.secret_nonce,
                        row.wrapped_dek,
                        row.dek_nonce,
                        kek,
                        connection_aad(ctx.user_id, PROVIDER, row.id),
                        connection_wrap_aad(kek.key_id, ctx.user_id, PROVIDER, row.id),
                    )
                except DecryptFailed:
                    failure = "decrypt_failed"
            parsed: dict[str, Any] | None = None
            if payload is not None:
                try:
                    loaded = json.loads(payload)
                except ValueError:
                    loaded = None
                if (
                    isinstance(loaded, dict)
                    and isinstance(loaded.get("access_token"), str)
                    and isinstance(loaded.get("refresh_token"), str)
                ):
                    parsed = loaded
                else:
                    failure = "decrypt_failed"
            if parsed is not None:
                opened = OpenWithings(
                    id=row.id,
                    generation=int(row.token_generation),
                    status=row.status,
                    access_token=parsed["access_token"],
                    refresh_token=parsed["refresh_token"],
                    public=_as_public(row.public),
                    intent=None if row.refresh_in_flight is None else int(row.refresh_in_flight),
                    stale_intent=bool(row.stale),
                )
            if failure == "decrypt_failed" and row is not None:
                await db.execute(
                    update(connections)
                    .where(
                        _C.id == row.id,
                        _C.owner_id == ctx.user_id,
                        _C.token_generation == row.token_generation,
                    )
                    .values(
                        status="needs_reconnect", last_error="decrypt_failed", updated_at=func.now()
                    )
                )
    if opened is None:
        raise ConnectionFailure(
            failure if failure in ("key_unavailable", "not_connected") else "decrypt_failed"
        )
    return opened


async def store_grant(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    app_id: int,
    userid: int,
    scope: str,
    access_token: str,
    refresh_token: str,
    *,
    expires_in: int,
) -> StoredGrant:
    """Upsert by ``(app_id, withings_userid)``. Another account is refused."""
    if type(app_id) is not int or app_id < 1 or type(userid) is not int or userid < 1:
        raise GrantRefused("conflict")
    if type(expires_in) is not int or not 1 <= expires_in <= 86_400:
        raise GrantRefused("conflict")
    access, refresh = _token(access_token), _token(refresh_token)
    scopes = _scope_list(scope)
    public = _public(app_id, userid, scopes)
    access_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    plaintext = _payload(access, refresh, access_expires_at)
    refused: str | None = None
    stored: StoredGrant | None = None
    async with user_session(factory, ctx) as db:
        kek = await ring.current(db)
        locked = (
            await db.execute(
                select(_C.id, _C.status, _C.token_generation, _C.public)
                .where(_C.owner_id == ctx.user_id, _C.provider == PROVIDER)
                .with_for_update()
            )
        ).all()
        same = [row for row in locked if _matches(_as_public(row.public), app_id, userid)]
        live = [row for row in locked if row.status != DISCONNECTED]
        if live and all(row.id != live[0].id for row in same):
            refused = "other_withings_account"
        elif same:
            target = next((row for row in same if row.status != DISCONNECTED), same[0])
            sealed = seal(
                plaintext,
                connection_aad(ctx.user_id, PROVIDER, target.id),
                kek,
                connection_wrap_aad(kek.key_id, ctx.user_id, PROVIDER, target.id),
            )
            if await write_tokens(
                db,
                ctx,
                target.id,
                int(target.token_generation),
                sealed,
                public=public,
                expires_at=datetime.now(UTC) + _REFRESH_TTL,
            ):
                stored = StoredGrant(target.id, int(target.token_generation) + 1, "connected")
            else:
                refused = "conflict"
        else:
            row_id = uuid4()
            sealed = seal(
                plaintext,
                connection_aad(ctx.user_id, PROVIDER, row_id),
                kek,
                connection_wrap_aad(kek.key_id, ctx.user_id, PROVIDER, row_id),
            )
            try:
                async with db.begin_nested():
                    await db.execute(
                        pg_insert(connections).values(
                            id=row_id,
                            owner_id=ctx.user_id,
                            provider=PROVIDER,
                            status="connected",
                            public=public,
                            secret=sealed.ciphertext,
                            secret_nonce=sealed.nonce,
                            wrapped_dek=sealed.wrapped_dek,
                            dek_nonce=sealed.dek_nonce,
                            key_id=sealed.key_id,
                            token_generation=1,
                            expires_at=datetime.now(UTC) + _REFRESH_TTL,
                        )
                    )
            except IntegrityError as exc:
                diag = getattr(exc.orig, "diag", None)
                name = getattr(diag, "constraint_name", "") or ""
                refused = (
                    "other_withings_account"
                    if name == "connections_one_live"
                    else "linked_elsewhere"
                )
            else:
                stored = StoredGrant(row_id, 1, "connected")
    if refused is not None or stored is None:
        raise GrantRefused(refused or "conflict")
    return stored


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
