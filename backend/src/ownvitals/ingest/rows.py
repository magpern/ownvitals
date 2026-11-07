"""Idempotent writes of the unpartitioned Withings tables.

A source insert, update or soft delete bumps ``data_revisions`` in the same transaction.
The same payload a second time changes nothing, so a resumed backfill does not ingest twice.
Unknown measure types are stored as sent. A NULL ECG signal is kept, with a ``signal_missing``
estimate, and a later write of only ``estimated`` does not bump the counter.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ingest.codec import (
    SignalRejected,
    canonical,
    check_rate,
    ecg_unchanged,
    frame_signal,
    missing_estimate,
    reported_body,
)
from ownvitals.ingest.revisions import bump
from ownvitals.ingest.rows_measures import measure_body, soft_delete_measure, upsert_measure_group
from ownvitals.ingest.rows_streams import (
    goal_body,
    upsert_activity,
    upsert_device,
    upsert_sleep_summary,
    upsert_workout,
)
from ownvitals.ingest.skips import INVALID, OUT_OF_RANGE, note
from ownvitals.ingest.when import from_epoch, in_range, zone_name
from ownvitals.withings.models import (
    HeartListEntry,
    HeartSignal,
)

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


__all__ = [
    "goal_body",
    "measure_body",
    "save_estimated",
    "soft_delete_measure",
    "upsert_activity",
    "upsert_device",
    "upsert_ecg",
    "upsert_measure_group",
    "upsert_sleep_summary",
    "upsert_workout",
]
