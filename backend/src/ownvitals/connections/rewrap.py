"""Move connection wraps onto the current KEK (ADR 0004 section 3).

Per user, as the app role with ``app.user_id`` set: one transaction per row. ``SELECT ... FOR
UPDATE``, unwrap under the row's KEK, wrap under the current one, then
``UPDATE ... WHERE id = :id AND key_id = :old``. Only the wrap changes. The payload and
``token_generation`` stay. A row with no credentials is not under a KEK and is left alone.
Counts are per key id. A row id is logged only when the wrap will not open.
"""

import logging
from collections import Counter
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.tables import connections
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import DecryptFailed, connection_wrap_aad, rewrap
from ownvitals.crypto.keks import KeyRing, KeyUnavailable
from ownvitals.db import user_session

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]
_C = connections.c


async def rows_by_key(factory: Factory, ctx: UserContext) -> Counter[str]:
    """How many of the user's credential rows each KEK id wraps."""
    async with user_session(factory, ctx) as db:
        rows = await db.execute(
            select(_C.key_id, func.count())
            .where(_C.owner_id == ctx.user_id, _C.key_id.is_not(None))
            .group_by(_C.key_id)
        )
        return Counter({str(row[0]): int(row[1]) for row in rows})


async def rewrap_owner(factory: Factory, ring: KeyRing, ctx: UserContext) -> Counter[str]:
    """Move every credential row of the user that is not under the current KEK onto it.

    Returns counts keyed ``"<old key id> <outcome>"``. Outcomes are ``rewrapped``,
    ``key_unavailable`` (the old KEK is missing or rejected; the row is unchanged),
    ``decrypt_failed`` (the row becomes ``needs_reconnect``) and ``skipped`` (the row changed,
    or it was already on the current KEK). Running it again finishes whatever the first run
    left, and does not wrap a row twice.
    """
    async with user_session(factory, ctx) as db:
        pending = (
            (
                await db.execute(
                    select(_C.id).where(
                        _C.owner_id == ctx.user_id,
                        _C.key_id.is_not(None),
                        _C.key_id != ring.current_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    counts: Counter[str] = Counter()
    for row_id in pending:
        old_key_id, outcome = await _rewrap_row(factory, ring, ctx, row_id)
        counts[f"{old_key_id} {outcome}"] += 1
    return counts


async def _rewrap_row(
    factory: Factory, ring: KeyRing, ctx: UserContext, row_id: UUID
) -> tuple[str, str]:
    async with user_session(factory, ctx) as db:
        current = await ring.current(db)
        row = (
            await db.execute(
                select(
                    _C.provider,
                    _C.key_id,
                    _C.wrapped_dek,
                    _C.dek_nonce,
                    _C.token_generation,
                )
                .where(
                    _C.id == row_id,
                    _C.owner_id == ctx.user_id,
                    _C.key_id.is_not(None),
                    _C.key_id != current.key_id,
                )
                .with_for_update()
            )
        ).first()
        if row is None or row.key_id is None:
            return "-", "skipped"
        try:
            old = await ring.kek_for(db, row.key_id)
        except KeyUnavailable:
            return str(row.key_id), "key_unavailable"
        moved: tuple[bytes, bytes] | None = None
        try:
            moved = rewrap(
                row.wrapped_dek,
                row.dek_nonce,
                old,
                connection_wrap_aad(old.key_id, ctx.user_id, row.provider, row_id),
                current,
                connection_wrap_aad(current.key_id, ctx.user_id, row.provider, row_id),
            )
        except DecryptFailed:
            moved = None
        if moved is None:
            await db.execute(
                update(connections)
                .where(
                    _C.id == row_id,
                    _C.owner_id == ctx.user_id,
                    _C.token_generation == row.token_generation,
                    _C.key_id == row.key_id,
                )
                .values(
                    status="needs_reconnect",
                    last_error="decrypt_failed",
                    updated_at=func.now(),
                )
            )
            logger.warning(
                "connection unusable",
                extra={"category": "decrypt_failed", "connection_id": str(row_id)},
            )
            return str(row.key_id), "decrypt_failed"
        result = await db.execute(
            update(connections)
            .where(_C.id == row_id, _C.owner_id == ctx.user_id, _C.key_id == row.key_id)
            .values(wrapped_dek=moved[0], dek_nonce=moved[1], key_id=current.key_id)
            .returning(_C.id)
        )
        return str(row.key_id), "rewrapped" if result.first() is not None else "skipped"
