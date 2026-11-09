"""Read and write connection rows. The only path from a row to plaintext tokens.

Every function takes a user context and runs in that user's session. Token writes go through
``write_tokens``: the generation must be the one just read, and the row must not be
disconnected unless the write is a reconnect, or the write changes nothing.
Plaintext never appears in a log record or an exception.

The reads without credentials live in ``rows.py`` and the opening and clearing of credentials
in ``credential_rows.py``; both are re-exported here, so this stays the one import path. This
module keeps the token write, connect, disconnect and the delete of a disconnected row.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.connections.credential_rows import clear_credentials as clear_credentials
from ownvitals.connections.credential_rows import open_credentials as open_credentials
from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.rows import _C, _CONNECTED, _INFO_COLUMNS, ConnectionInfo, Factory, _info
from ownvitals.connections.rows import connection_refs as connection_refs
from ownvitals.connections.rows import list_connections as list_connections
from ownvitals.connections.tables import DISCONNECTED, TEST_PROVIDER, connections
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import (
    Sealed,
    canonical_json,
    connection_aad,
    connection_wrap_aad,
    seal,
)
from ownvitals.crypto.keks import KeyRing
from ownvitals.db import user_session

logger = logging.getLogger(__name__)

_TOKEN_MIN = 8
_TOKEN_MAX = 256


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN_MIN <= len(value) <= _TOKEN_MAX:
        raise ConnectionFailure("unknown")
    return value


async def write_tokens(
    db: AsyncSession,
    ctx: UserContext,
    connection_id: UUID,
    generation: int,
    sealed: Sealed,
    *,
    public: dict[str, Any],
    expires_at: datetime | None,
    reconnect: bool = False,
) -> bool:
    """Store a new payload and raise the generation by one, or change nothing.

    ``False`` means ``generation`` is not the row's current one: a newer write won, and this
    one gives up (ADR 0004 section 6). The payload columns and the generation move together.

    A disconnect does not raise the generation, so a refresh that read the row before the
    disconnect still holds the current one. Only ``reconnect`` (a new grant from the user,
    ``store_grant``) may write a ``disconnected`` row; every other write then changes nothing,
    checked in the same statement, under the row lock the update takes.
    """
    filters = [
        _C.id == connection_id,
        _C.owner_id == ctx.user_id,
        _C.token_generation == generation,
    ]
    if not reconnect:
        filters.append(_C.status != DISCONNECTED)
    result = await db.execute(
        update(connections)
        .where(*filters)
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
    factory: Factory,
    ctx: UserContext,
    provider: str,
    *,
    revoke_failed: bool = False,
    connection_id: UUID | None = None,
    generation: int | None = None,
) -> bool:
    """Drop the live row's credentials and mark it disconnected. The row stays.

    ``revoke_failed`` stores that category on the row (ADR 0004 section 8). Otherwise
    ``last_error`` is cleared. ``False`` when the user has no live row for ``provider``
    (the route still answers 204). The generation is a token-write counter, so a
    disconnect does not change it (ADR 0004 section 6); ``write_tokens`` and the refresh's
    give-up refuse a disconnected row instead.

    ``connection_id`` limits it to that row, and ``generation`` to that token generation:
    a revoke that finishes late then leaves a newer connection (or the same row after a
    reconnect, which writes a new generation) alone.
    """
    filters = [_C.owner_id == ctx.user_id, _C.provider == provider, _C.status != DISCONNECTED]
    if connection_id is not None:
        filters.append(_C.id == connection_id)
    if generation is not None:
        filters.append(_C.token_generation == generation)
    async with user_session(factory, ctx) as db:
        row = (await db.execute(select(_C.id).where(*filters).with_for_update())).first()
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
