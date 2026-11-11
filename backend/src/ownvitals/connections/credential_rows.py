"""Open and clear the credentials on connection rows (``repository.py`` re-exports both).

``open_credentials`` decrypts one connected row or fails closed; ``clear_credentials`` removes
every credential of a user (``keys wipe``). Every function takes a user context and runs in that
user's session. Plaintext never appears in a log record or an exception.
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.connections.credentials import Credentials, OpenConnection, build_credentials
from ownvitals.connections.errors import CATEGORIES, ConnectionFailure
from ownvitals.connections.rows import _C, _CONNECTED, Factory
from ownvitals.connections.tables import DISCONNECTED, connections
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import (
    DecryptFailed,
    connection_aad,
    connection_wrap_aad,
    open_sealed,
)
from ownvitals.crypto.keks import KeyRing, KeyUnavailable
from ownvitals.db import user_session

# The logger keeps the name it had before this module was split out of ``repository.py``.
logger = logging.getLogger("ownvitals.connections.repository")


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
    that was read and the row is still connected (a disconnect keeps the generation, ADR
    0004 section 6), then raises. The failure is raised after the session commits.
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
                        _C.status == _CONNECTED,  # a disconnect keeps the generation
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

    One row per provider becomes ``needs_reconnect``: the provider's live row when it has one,
    otherwise its most recently updated row. Every other row of that provider stays (or
    becomes) ``disconnected``. Both states allow empty credentials, and the live unique index
    allows only one row that is not disconnected. The generation is not a token write, so it
    stays. Returns the providers that had a row. Running it again picks the same row.
    """
    rows = (
        await db.execute(
            select(_C.id, _C.provider, _C.updated_at)
            .where(_C.owner_id == ctx.user_id)
            .order_by(_C.provider, (_C.status == DISCONNECTED).asc(), _C.updated_at.desc(), _C.id)
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
