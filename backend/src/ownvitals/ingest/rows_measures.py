"""Idempotent writes of measure groups and their measures.

Split from ``ingest.rows`` (no behaviour change; its docstring describes the rules). A source
insert, update or soft delete bumps ``data_revisions`` in the same transaction.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ingest.codec import (
    canonical,
    payload_hash,
)
from ownvitals.ingest.revisions import bump
from ownvitals.ingest.rows_common import optional_instant
from ownvitals.ingest.skips import INVALID, OUT_OF_RANGE, note
from ownvitals.ingest.when import from_epoch, in_range, zone_name
from ownvitals.withings.models import (
    MeasureGroup,
)

_GROUP = text(
    "SELECT id, payload_hash, deleted_at FROM measure_groups "
    "WHERE owner_id = :owner AND connection_id = :connection AND grpid = :grpid"
)
_INSERT_GROUP = text(
    "INSERT INTO measure_groups (owner_id, connection_id, grpid, measured_at, source_tz, "
    "attrib, category, model_id, hash_deviceid, created_at, modified_at, raw, payload_hash) "
    "VALUES (:owner, :connection, :grpid, :at, :zone, :attrib, :category, :model, :device, "
    ":created, :modified, CAST(:raw AS jsonb), :hash) RETURNING id"
)
_UPDATE_GROUP = text(
    "UPDATE measure_groups SET measured_at = :at, source_tz = :zone, attrib = :attrib, "
    "category = :category, model_id = :model, hash_deviceid = :device, created_at = :created, "
    "modified_at = :modified, deleted_at = NULL, raw = CAST(:raw AS jsonb), payload_hash = :hash "
    "WHERE owner_id = :owner AND id = :id"
)
_DELETE_MEASURES = text(
    "DELETE FROM measures WHERE owner_id = :owner AND measure_group_id = :group"
)
_INSERT_MEASURE = text(
    "INSERT INTO measures (owner_id, measure_group_id, type, value_raw, unit, position) "
    "VALUES (:owner, :group, :type, :value, :unit, :position)"
)
_SOFT = text(
    "UPDATE measure_groups SET deleted_at = now() WHERE owner_id = :owner "
    "AND connection_id = :connection AND grpid = :grpid AND deleted_at IS NULL RETURNING id"
)


def measure_body(group: MeasureGroup, zone: str) -> dict[str, Any]:
    measures = [
        {"position": item.position, "type": item.type, "unit": item.unit, "value": item.value}
        for item in group.measures
    ]
    return {
        "attrib": group.attrib,
        "category": group.category,
        "date": group.date,
        "grpid": group.grpid,
        "hash_deviceid": group.hash_deviceid,
        "measures": measures,
        "model_id": group.model_id,
        "raw": group.raw,
        "timezone": zone,
    }


async def _replace_measures(
    db: AsyncSession, owner: UUID, group_id: UUID, group: MeasureGroup
) -> None:
    await db.execute(_DELETE_MEASURES, {"owner": owner, "group": group_id})
    for item in group.measures:
        await db.execute(
            _INSERT_MEASURE,
            {
                "owner": owner,
                "group": group_id,
                "type": item.type,
                "value": item.value,
                "unit": item.unit,
                "position": item.position,
            },
        )


async def upsert_measure_group(
    db: AsyncSession,
    owner: UUID,
    connection: UUID,
    group: MeasureGroup,
    zone: str,
    now: datetime,
) -> str:
    """``inserted``, ``updated``, ``unchanged`` or ``skipped``."""
    instant = from_epoch(group.date)
    if not in_range(instant, now):
        note("measures", OUT_OF_RANGE)
        return "skipped"
    named = zone_name(group.timezone, zone)
    device = group.hash_deviceid
    if device is not None and not 1 <= len(device) <= 128:
        note("measures", INVALID)
        return "skipped"
    body = measure_body(group, named)
    digest = payload_hash(body)
    existing = (
        await db.execute(_GROUP, {"owner": owner, "connection": connection, "grpid": group.grpid})
    ).first()
    if (
        existing is not None
        and bytes(existing.payload_hash) == digest
        and existing.deleted_at is None
    ):
        return "unchanged"
    values = {
        "owner": owner,
        "connection": connection,
        "grpid": group.grpid,
        "at": instant,
        "zone": named,
        "attrib": group.attrib,
        "category": group.category,
        "model": group.model_id,
        "device": device,
        "created": optional_instant(group.created, now),
        "modified": optional_instant(group.modified, now),
        "raw": json.dumps(canonical(body)),
        "hash": digest,
    }
    if existing is None:
        group_id = (await db.execute(_INSERT_GROUP, values)).scalar_one()
    else:
        group_id = existing.id
        await db.execute(_UPDATE_GROUP, {**values, "id": group_id})
    await _replace_measures(db, owner, group_id, group)
    await bump(db, owner, "measures")
    return "inserted" if existing is None else "updated"


async def soft_delete_measure(db: AsyncSession, owner: UUID, connection: UUID, grpid: int) -> bool:
    result = await db.execute(_SOFT, {"owner": owner, "connection": connection, "grpid": grpid})
    if result.first() is not None:
        await bump(db, owner, "measures")
        return True
    return False
