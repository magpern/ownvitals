"""Read and write connection rows. The only path from a row to plaintext tokens.

Every function takes a user context and runs in that user's session. Token writes go through
``write_tokens``: the generation must be the one just read, or the write changes nothing.
Plaintext never appears in a log record or an exception.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.credentials import Credentials, OpenConnection, build_credentials
from ownvitals.connections.errors import CATEGORIES, ConnectionFailure
from ownvitals.connections.tables import DISCONNECTED, TEST_PROVIDER, connections
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import (
    DecryptFailed,
    Sealed,
    canonical_json,
    connection_aad,
    connection_wrap_aad,
    open_sealed,
    seal,
)
from ownvitals.crypto.keks import KeyRing, KeyUnavailable
from ownvitals.db import user_session

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]
_C = connections.c
_TOKEN_MIN = 8
_TOKEN_MAX = 256
_CONNECTED = "connected"


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    """A row without its credentials."""

    id: UUID
    provider: str
    status: str
    last_error: str | None
    token_generation: int
    connected_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


def _info(row: Any) -> ConnectionInfo:
    return ConnectionInfo(
        id=row.id,
        provider=row.provider,
        status=row.status,
        last_error=row.last_error,
        token_generation=int(row.token_generation),
        connected_at=row.connected_at,
        updated_at=row.updated_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
    )


_INFO_COLUMNS = (
    _C.id,
    _C.provider,
    _C.status,
    _C.last_error,
    _C.token_generation,
    _C.connected_at,
    _C.updated_at,
    _C.last_used_at,
    _C.expires_at,
)


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN_MIN <= len(value) <= _TOKEN_MAX:
        raise ConnectionFailure("unknown")
    return value


async def connection_refs(factory: Factory, ctx: UserContext) -> frozenset[tuple[str, UUID]]:
    """The user's live ``(provider, connection id)`` pairs. No secrets and no status.

    A disconnected row is not a ref: a new connection for that provider has its own id.
    """
    async with user_session(factory, ctx) as db:
        rows = (
            await db.execute(
                select(_C.provider, _C.id).where(
                    _C.owner_id == ctx.user_id, _C.status != DISCONNECTED
                )
            )
        ).all()
    return frozenset((row.provider, row.id) for row in rows)


async def list_connections(factory: Factory, ctx: UserContext) -> list[ConnectionInfo]:
    """Every row of the user, oldest connection first. Credentials stay in the table."""
    async with user_session(factory, ctx) as db:
        rows = (
            await db.execute(
                select(*_INFO_COLUMNS)
                .where(_C.owner_id == ctx.user_id)
                .order_by(_C.connected_at, _C.id)
            )
        ).all()
    return [_info(row) for row in rows]


async def write_tokens(
    db: AsyncSession,
    ctx: UserContext,
    connection_id: UUID,
    generation: int,
    sealed: Sealed,
    *,
    public: dict[str, Any],
    expires_at: datetime | None,
) -> bool:
    """Store a new payload and raise the generation by one, or change nothing.

    ``False`` means ``generation`` is not the row's current one: a newer write won, and this
    one gives up (ADR 0004 section 6). The payload columns and the generation move together.
    """
    result = await db.execute(
        update(connections)
        .where(
            _C.id == connection_id,
            _C.owner_id == ctx.user_id,
            _C.token_generation == generation,
        )
        .values(
            status=_CONNECTED,
            last_error=None,
            public=public,
            secret=sealed.ciphertext,
            secret_nonce=sealed.nonce,
            wrapped_dek=sealed.wrapped_dek,
            dek_nonce=sealed.dek_nonce,
            key_id=sealed.key_id,
            expires_at=expires_at,
            refresh_in_flight=None,
            refresh_intent_at=None,
            token_generation=generation + 1,
            connected_at=func.now(),
            updated_at=func.now(),
        )
        .returning(_C.id)
    )
    return result.first() is not None


def _seal(owner_id: UUID, provider: str, row_id: UUID, plaintext: bytes, kek: Any) -> Sealed:
    return seal(
        plaintext,
        connection_aad(owner_id, provider, row_id),
        kek,
        connection_wrap_aad(kek.key_id, owner_id, provider, row_id),
    )


async def connect(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    provider: str,
    access_token: str,
    refresh_token: str,
) -> ConnectionInfo:
    """Store the test provider's tokens on the user's live row, or on a new row.

    A disconnected row is left in place. The provider value is accepted only for the test
    provider; Withings arrives in M1. The plaintext is sealed inside the transaction, after
    the row id is known, under a new data key.
    """
    if provider != TEST_PROVIDER:
        raise ConnectionFailure("unknown")
    access, refresh = _token(access_token), _token(refresh_token)
    plaintext = canonical_json({"access_token": access, "refresh_token": refresh})
    async with user_session(factory, ctx) as db:
        kek = await ring.current(db)
        locked = (
            await db.execute(
                select(_C.id, _C.status, _C.token_generation)
                .where(_C.owner_id == ctx.user_id, _C.provider == provider)
                .with_for_update()
            )
        ).all()
        live = [row for row in locked if row.status != DISCONNECTED]
        stored: UUID | None = None
        if live:
            current = live[0]
            sealed = _seal(ctx.user_id, provider, current.id, plaintext, kek)
            if await write_tokens(
                db,
                ctx,
                current.id,
                int(current.token_generation),
                sealed,
                public={},
                expires_at=None,
            ):
                stored = current.id
        else:
            row_id = uuid4()
            sealed = _seal(ctx.user_id, provider, row_id, plaintext, kek)
            duplicated = False
            try:
                await db.execute(
                    pg_insert(connections).values(
                        id=row_id,
                        owner_id=ctx.user_id,
                        provider=provider,
                        status=_CONNECTED,
                        public={},
                        secret=sealed.ciphertext,
                        secret_nonce=sealed.nonce,
                        wrapped_dek=sealed.wrapped_dek,
                        dek_nonce=sealed.dek_nonce,
                        key_id=sealed.key_id,
                        token_generation=1,
                    )
                )
            except IntegrityError:
                duplicated = True
            if not duplicated:
                stored = row_id
        if stored is None:
            raise ConnectionFailure("unknown")
        row = (
            await db.execute(
                select(*_INFO_COLUMNS).where(_C.id == stored, _C.owner_id == ctx.user_id)
            )
        ).one()
    logger.info(
        "connection stored",
        extra={"connection_id": str(stored), "provider": provider},
    )
    return _info(row)


async def disconnect(
    factory: Factory, ctx: UserContext, provider: str, *, revoke_failed: bool = False
) -> bool:
    """Drop the live row's credentials and mark it disconnected. The row stays.

    ``revoke_failed`` stores that category on the row (ADR 0004 section 8). Otherwise
    ``last_error`` is cleared. ``False`` when the user has no live row for ``provider``
    (the route still answers 204). The generation is a token-write counter, so a
    disconnect does not change it.
    """
    async with user_session(factory, ctx) as db:
        row = (
            await db.execute(
                select(_C.id)
                .where(
                    _C.owner_id == ctx.user_id,
                    _C.provider == provider,
                    _C.status != DISCONNECTED,
                )
                .with_for_update()
            )
        ).first()
        if row is None:
            return False
        await db.execute(
            update(connections)
            .where(_C.id == row.id, _C.owner_id == ctx.user_id)
            .values(
                status=DISCONNECTED,
                secret=None,
                secret_nonce=None,
                wrapped_dek=None,
                dek_nonce=None,
                key_id=None,
                last_error="revoke_failed" if revoke_failed else None,
                refresh_in_flight=None,
                refresh_intent_at=None,
                updated_at=func.now(),
            )
        )
        connection_id = row.id
    logger.info(
        "connection disconnected",
        extra={"connection_id": str(connection_id), "provider": provider},
    )
    return True


async def delete_disconnected_row(factory: Factory, ctx: UserContext, connection_id: UUID) -> None:
    """Delete this user's disconnected row so its Withings account can be linked again.

    A live row is left in place. Credential columns are not written here.
    """
    async with user_session(factory, ctx) as db:
        await db.execute(
            delete(connections).where(
                _C.id == connection_id,
                _C.owner_id == ctx.user_id,
                _C.status == DISCONNECTED,
            )
        )


async def _load_for_open(db: AsyncSession, ctx: UserContext, connection_id: UUID) -> Any:
    return (
        await db.execute(
            select(
                _C.id,
                _C.provider,
                _C.status,
                _C.token_generation,
                _C.secret,
                _C.secret_nonce,
                _C.wrapped_dek,
                _C.dek_nonce,
                _C.key_id,
            )
            .where(_C.id == connection_id, _C.owner_id == ctx.user_id)
            .with_for_update()
        )
    ).first()


def _parse_payload(provider: str, payload: bytes) -> Credentials | None:
    parsed: Any = None
    broken = False
    try:
        parsed = json.loads(payload)
    except ValueError:
        broken = True
    if broken or not isinstance(parsed, dict):
        return None
    try:
        return build_credentials(provider, parsed)
    except ValueError:
        return None


async def open_credentials(
    factory: Factory, ring: KeyRing, ctx: UserContext, connection_id: UUID
) -> OpenConnection:
    """Decrypt one connected row, or fail closed.

    A missing key leaves the row unchanged (``key_unavailable``). A GCM failure sets
    ``needs_reconnect`` and ``decrypt_failed`` only when the generation is still the one
    that was read, then raises. The failure is raised after the session commits.
    """
    failure: str | None = None
    opened: OpenConnection | None = None
    async with user_session(factory, ctx) as db:
        row = await _load_for_open(db, ctx, connection_id)
        if row is None or row.status != _CONNECTED or row.secret is None or row.key_id is None:
            failure = "not_connected"
        else:
            kek = None
            try:
                kek = await ring.kek_for(db, row.key_id)
            except KeyUnavailable:
                failure = "key_unavailable"
            payload: bytes | None = None
            if kek is not None:
                try:
                    payload = open_sealed(
                        row.secret,
                        row.secret_nonce,
                        row.wrapped_dek,
                        row.dek_nonce,
                        kek,
                        connection_aad(ctx.user_id, row.provider, row.id),
                        connection_wrap_aad(kek.key_id, ctx.user_id, row.provider, row.id),
                    )
                except DecryptFailed:
                    failure = "decrypt_failed"
            if payload is not None:
                credentials = _parse_payload(row.provider, payload)
                if credentials is None:
                    failure = "decrypt_failed"
                else:
                    opened = OpenConnection(
                        id=row.id,
                        provider=row.provider,
                        credentials=credentials,
                        generation=int(row.token_generation),
                    )
            if failure == "decrypt_failed":
                await db.execute(
                    update(connections)
                    .where(
                        _C.id == row.id,
                        _C.owner_id == ctx.user_id,
                        _C.token_generation == row.token_generation,
                    )
                    .values(
                        status="needs_reconnect",
                        last_error="decrypt_failed",
                        updated_at=func.now(),
                    )
                )
                logger.warning(
                    "connection unusable",
                    extra={"category": "decrypt_failed", "connection_id": str(row.id)},
                )
    if opened is None:
        category = failure if failure in CATEGORIES else "decrypt_failed"
        raise ConnectionFailure(category)
    return opened


async def clear_credentials(db: AsyncSession, ctx: UserContext) -> tuple[str, ...]:
    """Remove every credential and keep the rows (``keys wipe``).

    One row per provider becomes ``needs_reconnect``; any older row of that provider stays
    ``disconnected``. Both states allow empty credentials, and the live unique index allows
    only one row that is not disconnected. The generation is not a token write, so it stays.
    Returns the providers that had a row.
    """
    rows = (
        await db.execute(
            select(_C.id, _C.provider, _C.updated_at)
            .where(_C.owner_id == ctx.user_id)
            .order_by(_C.provider, _C.updated_at.desc(), _C.id)
            .with_for_update()
        )
    ).all()
    keep: dict[str, UUID] = {}
    for row in rows:
        keep.setdefault(row.provider, row.id)
    # Empty every row first. The live unique index is checked per statement, so marking the
    # keeper ``needs_reconnect`` while another row of that provider is still connected would
    # be two live rows.
    cleared = {
        "status": DISCONNECTED,
        "secret": None,
        "secret_nonce": None,
        "wrapped_dek": None,
        "dek_nonce": None,
        "key_id": None,
        "last_error": None,
        "refresh_in_flight": None,
        "refresh_intent_at": None,
        "updated_at": func.now(),
    }
    for row in rows:
        await db.execute(
            update(connections).where(_C.id == row.id, _C.owner_id == ctx.user_id).values(**cleared)
        )
    for row_id in keep.values():
        await db.execute(
            update(connections)
            .where(_C.id == row_id, _C.owner_id == ctx.user_id)
            .values(status="needs_reconnect", updated_at=func.now())
        )
    return tuple(sorted(keep))
