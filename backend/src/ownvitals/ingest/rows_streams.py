"""Idempotent writes of the activity, workout, sleep summary and device tables, and the goals
body.

Split from ``ingest.rows`` (no behaviour change; its docstring describes the rules). A source
insert or update bumps ``data_revisions`` in the same transaction.
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ingest.codec import (
    ACTIVITY_FIELDS,
    canonical,
    payload_hash,
    plain_object,
)
from ownvitals.ingest.revisions import bump
from ownvitals.ingest.rows_common import changed, optional_instant
from ownvitals.ingest.skips import INVALID, OUT_OF_RANGE, note
from ownvitals.ingest.when import from_epoch, in_range, local_day, zone_name
from ownvitals.withings.models import (
    Activity,
    Device,
    SleepSummary,
    Workout,
)

_ACTIVITY = text(
    "INSERT INTO activity_daily (owner_id, connection_id, day, start_at, source_tz, raw, "
    "payload_hash) VALUES (:owner, :connection, :day, :start, :zone, CAST(:raw AS jsonb), :hash) "
    "ON CONFLICT (owner_id, connection_id, day) DO UPDATE SET start_at = EXCLUDED.start_at, "
    "source_tz = EXCLUDED.source_tz, raw = EXCLUDED.raw, payload_hash = EXCLUDED.payload_hash "
    "WHERE activity_daily.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash RETURNING id"
)


async def upsert_activity(
    db: AsyncSession, owner: UUID, connection: UUID, row: Activity, zone: str, now: datetime
) -> str:
    named = zone_name(row.timezone, zone)
    try:
        day = datetime.fromisoformat(row.date).date()
    except ValueError:
        note("activity_daily", INVALID)
        return "skipped"
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if not in_range(start, now):
        note("activity_daily", OUT_OF_RANGE)
        return "skipped"
    kept = {name: getattr(row, name) for name in ACTIVITY_FIELDS if getattr(row, name) is not None}
    body = {"date": row.date, "fields": kept, "raw": row.raw, "timezone": named}
    found = (
        await db.execute(
            _ACTIVITY,
            {
                "owner": owner,
                "connection": connection,
                "day": day,
                "start": start,
                "zone": named,
                "raw": json.dumps(canonical(body)),
                "hash": payload_hash(body),
            },
        )
    ).first()
    if not changed(found):
        return "unchanged"
    await bump(db, owner, "activity_daily")
    return "inserted"


_WORKOUT = text(
    "INSERT INTO workouts (owner_id, connection_id, workout_id, start_at, end_at, source_tz, "
    "category, raw, payload_hash) VALUES (:owner, :connection, :workout, :start, :end, :zone, "
    ":category, CAST(:raw AS jsonb), :hash) ON CONFLICT (owner_id, connection_id, workout_id) "
    "DO UPDATE SET start_at = EXCLUDED.start_at, end_at = EXCLUDED.end_at, "
    "source_tz = EXCLUDED.source_tz, category = EXCLUDED.category, raw = EXCLUDED.raw, "
    "payload_hash = EXCLUDED.payload_hash WHERE workouts.payload_hash IS DISTINCT FROM "
    "EXCLUDED.payload_hash RETURNING id"
)


async def upsert_workout(
    db: AsyncSession, owner: UUID, connection: UUID, row: Workout, zone: str, now: datetime
) -> str:
    if row.startdate is None or row.enddate is None or row.enddate < row.startdate:
        note("workouts", INVALID)
        return "skipped"
    start, end = from_epoch(row.startdate), from_epoch(row.enddate)
    if not in_range(start, now) or not in_range(end, now):
        note("workouts", OUT_OF_RANGE)
        return "skipped"
    named = zone_name(row.timezone, zone)
    body = {
        "data": plain_object(row.data),
        "end": row.enddate,
        "id": row.id,
        "raw": row.raw,
        "start": row.startdate,
    }
    found = (
        await db.execute(
            _WORKOUT,
            {
                "owner": owner,
                "connection": connection,
                "workout": row.id,
                "start": start,
                "end": end,
                "zone": named,
                "category": row.category,
                "raw": json.dumps(canonical(body)),
                "hash": payload_hash(body),
            },
        )
    ).first()
    if not changed(found):
        return "unchanged"
    await bump(db, owner, "workouts")
    return "inserted"


_SUMMARY = text(
    "INSERT INTO sleep_summaries (owner_id, connection_id, day, start_at, end_at, source_tz, "
    "model_id, raw, payload_hash) VALUES (:owner, :connection, :day, :start, :end, :zone, "
    ":model, CAST(:raw AS jsonb), :hash) ON CONFLICT (owner_id, connection_id, day) DO UPDATE "
    "SET start_at = EXCLUDED.start_at, end_at = EXCLUDED.end_at, source_tz = EXCLUDED.source_tz, "
    "model_id = EXCLUDED.model_id, raw = EXCLUDED.raw, payload_hash = EXCLUDED.payload_hash "
    "WHERE sleep_summaries.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash RETURNING id"
)


async def upsert_sleep_summary(
    db: AsyncSession, owner: UUID, connection: UUID, row: SleepSummary, zone: str, now: datetime
) -> str:
    if row.startdate is None or row.enddate is None:
        note("sleep_summary", INVALID)
        return "skipped"
    start, end = from_epoch(row.startdate), from_epoch(row.enddate)
    if not in_range(start, now) or not in_range(end, now) or end < start:
        note("sleep_summary", OUT_OF_RANGE)
        return "skipped"
    named = zone_name(row.timezone, zone)
    if row.date:
        try:
            day = datetime.fromisoformat(row.date).date()
        except ValueError:
            note("sleep_summary", INVALID)
            return "skipped"
    else:
        day = local_day(start, named)
    body = {
        "data": plain_object(row.data),
        "end": row.enddate,
        "id": row.id,
        "raw": row.raw,
        "start": row.startdate,
    }
    found = (
        await db.execute(
            _SUMMARY,
            {
                "owner": owner,
                "connection": connection,
                "day": day,
                "start": start,
                "end": end,
                "zone": named,
                "model": row.model_id if row.model_id is not None else row.model,
                "raw": json.dumps(canonical(body)),
                "hash": payload_hash(body),
            },
        )
    ).first()
    if not changed(found):
        return "unchanged"
    await bump(db, owner, "sleep_summary")
    return "inserted"


_DEVICE = text(
    "INSERT INTO devices (owner_id, connection_id, hash_deviceid, model_id, type, battery, "
    "last_session_at, source_tz) VALUES (:owner, :connection, :device, :model, :kind, :battery, "
    ":session, :zone) ON CONFLICT (owner_id, connection_id, hash_deviceid) DO UPDATE SET "
    "model_id = EXCLUDED.model_id, type = EXCLUDED.type, battery = EXCLUDED.battery, "
    "last_session_at = EXCLUDED.last_session_at, source_tz = EXCLUDED.source_tz "
    "WHERE devices.model_id IS DISTINCT FROM EXCLUDED.model_id OR devices.type IS DISTINCT FROM "
    "EXCLUDED.type OR devices.battery IS DISTINCT FROM EXCLUDED.battery "
    "OR devices.last_session_at IS DISTINCT FROM EXCLUDED.last_session_at RETURNING id"
)


async def upsert_device(
    db: AsyncSession, owner: UUID, connection: UUID, row: Device, zone: str, now: datetime
) -> str:
    device = row.hash_deviceid or row.deviceid
    if not 1 <= len(device) <= 128:
        note("devices", INVALID)
        return "skipped"
    session = optional_instant(row.last_session_date, now)
    if row.last_session_date is not None and session is None:
        note("devices", OUT_OF_RANGE)
        return "skipped"
    found = (
        await db.execute(
            _DEVICE,
            {
                "owner": owner,
                "connection": connection,
                "device": device,
                "model": row.model_id,
                "kind": row.type,
                "battery": row.battery,
                "session": session,
                "zone": zone_name(row.timezone, zone) if session is not None else None,
            },
        )
    ).first()
    if not changed(found):
        return "unchanged"
    await bump(db, owner, "devices")
    return "inserted"


def goal_body(goals: Any) -> dict[str, Any]:
    weight = (
        None if goals.weight is None else {"unit": goals.weight.unit, "value": goals.weight.value}
    )
    return {"sleep": goals.sleep, "steps": goals.steps, "weight": weight}
