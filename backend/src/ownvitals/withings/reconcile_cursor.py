"""The sync cursor as nightly reconciliation (``reconcile.py``) reads and writes it."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ingest.cursors import write_cursor

_CURSOR = text(
    "SELECT capability, lastupdate, backfill_state, source_tz, covered_from, covered_to, "
    "last_ok_at FROM sync_cursors WHERE owner_id = :owner AND connection_id = :connection "
    "AND stream = :stream"
)


@dataclass
class _Cursor:
    capability: str | None
    lastupdate: int | None
    state: dict[str, Any]
    zone: str | None
    covered_from: datetime | None
    covered_to: datetime | None
    last_ok: datetime | None


async def _load(db: AsyncSession, owner: UUID, connection: UUID, stream: str) -> _Cursor:
    row = (
        await db.execute(_CURSOR, {"owner": owner, "connection": connection, "stream": stream})
    ).first()
    if row is None:
        return _Cursor(None, None, {}, None, None, None, None)
    state = row.backfill_state if isinstance(row.backfill_state, dict) else {}
    lastupdate = None if row.lastupdate is None else int(row.lastupdate)
    return _Cursor(
        row.capability, lastupdate, dict(state), row.source_tz, row.covered_from,
        row.covered_to, row.last_ok_at,
    )  # fmt: skip


async def _save(
    db: AsyncSession,
    owner: UUID,
    connection: UUID,
    stream: str,
    cursor: _Cursor,
    *,
    capability: str | None,
    state: dict[str, Any],
    zone: str | None,
    last_ok: datetime | None,
) -> None:
    await write_cursor(
        db, owner, connection, stream, capability=capability, state=state,
        lastupdate=cursor.lastupdate, covered_from=cursor.covered_from,
        covered_to=cursor.covered_to, last_ok=last_ok,
        zone=zone if zone is not None else cursor.zone,
    )  # fmt: skip
