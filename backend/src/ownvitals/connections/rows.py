"""Connection rows without their credentials (``repository.py`` re-exports what is public here).

Every function takes a user context and runs in that user's session; no secret column is read.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.tables import DISCONNECTED, connections
from ownvitals.context import UserContext
from ownvitals.db import user_session

Factory = async_sessionmaker[AsyncSession]
_C = connections.c


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
