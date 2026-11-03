"""The live connections' states, read in the caller's transaction (#187).

The rule evaluation's Q1 reads which connections are live and whether one needs renewing (ADR
0010 §7, PLAN §4.3) inside its own read and write transactions, so it cannot use the
repository's functions, which open their own session. This is the one read it needs: ids and
statuses only, never a credential column, filtered by ``owner_id`` and under the caller's RLS.
The ``connections`` table stays inside ``ownvitals.connections`` (ADR 0004 section 5, the crypto
guard).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.connections.tables import DISCONNECTED, connections

_C = connections.c


@dataclass(frozen=True, slots=True)
class LiveConnection:
    id: UUID
    status: str


async def live_connections(db: AsyncSession, owner: UUID) -> list[LiveConnection]:
    """The owner's connections that are not disconnected, by id."""
    rows = (
        await db.execute(
            select(_C.id, _C.status)
            .where(_C.owner_id == owner, _C.status != DISCONNECTED)
            .order_by(_C.id)
        )
    ).all()
    return [LiveConnection(row.id, str(row.status)) for row in rows]
