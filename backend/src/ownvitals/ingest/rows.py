"""Idempotent writes of the unpartitioned Withings tables.

A source insert, update or soft delete bumps ``data_revisions`` in the same transaction.
The same payload a second time changes nothing, so a resumed backfill does not ingest twice.
Unknown measure types are stored as sent. A NULL ECG signal is kept, with a ``signal_missing``
estimate, and a later write of only ``estimated`` does not bump the counter.
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ingest.codec import (
    ACTIVITY_FIELDS,
    SignalRejected,
    canonical,
    check_rate,
    ecg_unchanged,
    frame_signal,
    missing_estimate,
    payload_hash,
    plain_object,
    reported_body,
)
from ownvitals.ingest.revisions import bump
from ownvitals.ingest.skips import INVALID, OUT_OF_RANGE, note
from ownvitals.ingest.when import from_epoch, in_range, local_day, zone_name
from ownvitals.withings.models import (
    Activity,
    Device,
    HeartListEntry,
    HeartSignal,
    MeasureGroup,
    SleepSummary,
    Workout,
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


def _optional_instant(epoch: int | None, now: datetime) -> datetime | None:
    if epoch is None:
        return None
    instant = from_epoch(epoch)
    return instant if in_range(instant, now) else None


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
        "created": _optional_instant(group.created, now),
        "modified": _optional_instant(group.modified, now),
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


_ECG_ROW = text(
    "SELECT payload_hash, signal IS NULL AS missing, recorded_at, source_tz, model_id, afib, "
    "heart_rate, sampling_hz, wearposition, reported, afib_details FROM ecg_recordings "
    "WHERE owner_id = :owner AND connection_id = :connection AND signalid = :signalid"
)
_INSERT_ECG = text(
    "INSERT INTO ecg_recordings (owner_id, connection_id, signalid, recorded_at, source_tz, "
    "model_id, afib, heart_rate, sampling_hz, wearposition, signal, payload_hash, reported, "
    "estimated, afib_details) VALUES (:owner, :connection, :signalid, :at, :zone, :model, "
    ":afib, :heart, :rate, :wear, :signal, :hash, CAST(:reported AS jsonb), "
    "CAST(:estimated AS jsonb), CAST(:details AS jsonb))"
)
_UPDATE_ECG = text(
    "UPDATE ecg_recordings SET recorded_at = :at, source_tz = :zone, model_id = :model, "
    "afib = :afib, heart_rate = :heart, sampling_hz = :rate, wearposition = :wear, "
    "signal = :signal, payload_hash = :hash, reported = CAST(:reported AS jsonb), "
    "estimated = CAST(:estimated AS jsonb), afib_details = CAST(:details AS jsonb) "
    "WHERE owner_id = :owner AND connection_id = :connection AND signalid = :signalid"
)
_ESTIMATED_ONLY = text(
    "UPDATE ecg_recordings SET estimated = CAST(:estimated AS jsonb) "
    "WHERE owner_id = :owner AND connection_id = :connection AND signalid = :signalid"
)


async def upsert_ecg(
    db: AsyncSession,
    owner: UUID,
    connection: UUID,
    entry: HeartListEntry,
    signal: HeartSignal | None,
    zone: str,
    now: datetime,
) -> str:
    """Store one listed recording. A signal outside the contract is kept with ``signal`` NULL."""
    if entry.ecg is None:
        note("ecg", INVALID)
        return "skipped"
    instant = from_epoch(entry.timestamp)
    if not in_range(instant, now):
        note("ecg", OUT_OF_RANGE)
        return "skipped"
    signalid = entry.ecg.signalid
    frame: bytes | None = None
    digest: bytes | None = None
    rate: int | None = None
    wear = None
    model = entry.model
    heart = entry.heart_rate
    rejected = signal is None
    if signal is not None:
        try:
            rate = check_rate(signal.sampling_frequency)
            frame, digest = frame_signal([list(signal.signal)])
        except SignalRejected:
            rejected = True
            frame = None
            digest = None
        else:
            wear = signal.wearposition
            if signal.model is not None:
                model = signal.model
            if signal.heart_rate is not None and signal.heart_rate.value is not None:
                heart = signal.heart_rate.value
    existing = (
        await db.execute(_ECG_ROW, {"owner": owner, "connection": connection, "signalid": signalid})
    ).first()
    details = (
        None if entry.ecg.afib_details is None else json.dumps({"code": entry.ecg.afib_details})
    )
    values = {
        "owner": owner,
        "connection": connection,
        "signalid": signalid,
        "at": instant,
        "zone": zone_name(entry.timezone, zone),
        "model": model,
        "afib": entry.ecg.afib,
        "heart": heart,
        "rate": rate,
        "wear": wear,
        "signal": frame,
        "hash": digest,
        "reported": json.dumps(canonical(reported_body(entry))),
        "estimated": None,
        "details": details,
    }
    if ecg_unchanged(existing, values, rejected=rejected, digest=digest):
        return "unchanged"
    revision = await bump(db, owner, "ecg")
    if rejected:
        values["estimated"] = json.dumps(missing_estimate(signalid, rate, revision))
    await db.execute(_INSERT_ECG if existing is None else _UPDATE_ECG, values)
    return "inserted" if existing is None else "updated"


async def save_estimated(
    db: AsyncSession,
    owner: UUID,
    connection: UUID,
    signalid: int,
    estimated: dict[str, Any],
) -> None:
    """Derived output only. The revision counter stays where the source row left it."""
    await db.execute(
        _ESTIMATED_ONLY,
        {
            "owner": owner,
            "connection": connection,
            "signalid": signalid,
            "estimated": json.dumps(estimated),
        },
    )


def _changed(found: Any) -> bool:
    return found is not None


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
    kept = {
        name: getattr(row, name) for name in ACTIVITY_FIELDS if getattr(row, name) is not None
    }
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
    if not _changed(found):
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
    if not _changed(found):
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
    if not _changed(found):
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
    session = _optional_instant(row.last_session_date, now)
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
    if not _changed(found):
        return "unchanged"
    await bump(db, owner, "devices")
    return "inserted"


def goal_body(goals: Any) -> dict[str, Any]:
    weight = (
        None if goals.weight is None else {"unit": goals.weight.unit, "value": goals.weight.value}
    )
    return {"sleep": goals.sleep, "steps": goals.steps, "weight": weight}
