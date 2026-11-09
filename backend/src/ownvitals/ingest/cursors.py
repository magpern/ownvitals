"""``sync_cursors``: the covered range, the capability and the resumable backfill state.

``backfill_state`` is a JSON object. It holds the next window index, how many rows were
skipped as out of range, and, for goals, the latest payload. There is no goals table (the
schema from the measures issue has none), so the goals stream keeps that payload here, under
the owner's row, and nowhere else. Values in it are not logged.

The covered range only widens: a write with no range (an incremental pass, a classified stream)
keeps the stored one, and a later write extends it (``LEAST``/``GREATEST`` skip NULL).
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_READ = text(
    "SELECT capability, lastupdate, backfill_state, source_tz "
    "FROM sync_cursors WHERE owner_id = :owner AND connection_id = :connection AND stream = :stream"
)
_UPSERT = text(
    "INSERT INTO sync_cursors (owner_id, connection_id, stream, capability, lastupdate, "
    "covered_from, covered_to, backfill_state, last_ok_at, last_error_category, source_tz) "
    "VALUES (:owner, :connection, :stream, :capability, :lastupdate, :covered_from, :covered_to, "
    "CAST(:state AS jsonb), :last_ok, :error, :zone) "
    "ON CONFLICT (owner_id, connection_id, stream) DO UPDATE SET "
    "capability = EXCLUDED.capability, lastupdate = EXCLUDED.lastupdate, "
    "covered_from = LEAST(sync_cursors.covered_from, EXCLUDED.covered_from), "
    "covered_to = GREATEST(sync_cursors.covered_to, EXCLUDED.covered_to), "
    "backfill_state = EXCLUDED.backfill_state, last_ok_at = EXCLUDED.last_ok_at, "
    "last_error_category = EXCLUDED.last_error_category, source_tz = EXCLUDED.source_tz"
)


async def read_cursor(
    db: AsyncSession, owner: UUID, connection: UUID, stream: str
) -> dict[str, Any]:
    row = (
        await db.execute(_READ, {"owner": owner, "connection": connection, "stream": stream})
    ).first()
    if row is None:
        return {"capability": None, "lastupdate": None, "state": {}, "zone": None}
    state = row.backfill_state if isinstance(row.backfill_state, dict) else {}
    return {
        "capability": row.capability,
        "lastupdate": row.lastupdate,
        "state": state,
        "zone": row.source_tz,
    }


async def write_cursor(
    db: AsyncSession,
    owner: UUID,
    connection: UUID,
    stream: str,
    *,
    capability: str | None,
    state: dict[str, Any],
    lastupdate: int | None = None,
    covered_from: datetime | None = None,
    covered_to: datetime | None = None,
    last_ok: datetime | None = None,
    error: str | None = None,
    zone: str | None = None,
) -> None:
    await db.execute(
        _UPSERT,
        {
            "owner": owner,
            "connection": connection,
            "stream": stream,
            "capability": capability,
            "lastupdate": lastupdate,
            "covered_from": covered_from,
            "covered_to": covered_to,
            "state": json.dumps(state),
            "last_ok": last_ok,
            "error": error,
            "zone": zone,
        },
    )
