"""The Q1 state of the rule evaluation (#187): connections, streams and devices, read for one pass.

Split from ``findings_gather`` (no behaviour change). Reads ``sync_cursors``, ``devices`` and
``jobs`` for the caller's connections; touches no ``findings`` row.
"""

from collections import defaultdict
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.analysis.rules.system import ConnectionState, DeviceState, StreamState
from ownvitals.connections.state import live_connections
from ownvitals.derived.evaluate import Q1State

STREAM: Final = "measures"
SUPPORTED: Final = "supported"
# A sync job that is importing a connection's history (Q1's "not while importing").
IMPORT_KINDS: Final = ("withings_backfill", "withings_connect")
_CURSORS = text(
    "SELECT connection_id, stream, capability, backfill_state->>'status' AS state "
    "FROM sync_cursors WHERE owner_id = :o AND connection_id = ANY(CAST(:c AS uuid[]))"
)
_DEVICES = text(
    "SELECT hash_deviceid, battery FROM devices WHERE owner_id = :o "
    "AND connection_id = ANY(CAST(:c AS uuid[]))"
)
_IMPORTING = text(
    "SELECT input->>'connection_id' AS connection, input->>'stream' AS stream FROM jobs "
    "WHERE owner_id = :o AND state IN ('queued', 'running') "
    "AND kind = ANY(CAST(:kinds AS text[]))"
)


async def q1_state(db: AsyncSession, owner: UUID) -> Q1State:
    connections = await live_connections(db, owner)
    ids = [r.id for r in connections]
    if not ids:
        return Q1State()
    cursors = (await db.execute(_CURSORS, {"o": owner, "c": ids})).all()
    devices = (await db.execute(_DEVICES, {"o": owner, "c": ids})).all()
    jobs = (await db.execute(_IMPORTING, {"o": owner, "kinds": list(IMPORT_KINDS)})).all()
    importing = {(j.connection, j.stream) for j in jobs}
    reconnect = any(r.status == "needs_reconnect" for r in connections)
    status = "needs_reconnect" if reconnect else "connected"
    by_stream: dict[str, list[Any]] = defaultdict(list)
    for cursor in cursors:
        by_stream[str(cursor.stream)].append(cursor)
    streams: list[StreamState] = []
    for name, rows in sorted(by_stream.items()):
        supported = [r for r in rows if r.capability == SUPPORTED]
        capability = SUPPORTED if supported else (rows[0].capability if len(rows) == 1 else None)
        streams.append(
            StreamState(
                stream=name,
                capability=capability,
                complete=all(r.state == "complete" for r in supported or rows),
                importing=any(
                    (str(r.connection_id), name) in importing
                    or (str(r.connection_id), None) in importing
                    for r in rows
                ),
            )
        )
    measures = next((s for s in streams if s.stream == STREAM), None)
    batteries: dict[str, str | None] = {}
    for device in devices:
        name = str(device.hash_deviceid)
        if batteries.get(name) != "low":  # one device on two connections: low wins
            batteries[name] = None if device.battery is None else str(device.battery)
    return Q1State(
        weight_capability=None if measures is None else measures.capability,
        connection=ConnectionState(status),
        devices=tuple(DeviceState(name, battery) for name, battery in sorted(batteries.items())),
        streams=tuple(streams),
    )
