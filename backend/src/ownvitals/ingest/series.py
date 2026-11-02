"""Rows of the monthly tables. The caller creates each month in its own transaction first.

A sample the API left out is not written as zero. A timestamp outside the storage window is
counted as out of range and dropped, with no value logged. Unchanged samples do not bump
``data_revisions``.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ingest.codec import fixed_point
from ownvitals.ingest.revisions import bump
from ownvitals.ingest.skips import INVALID, OUT_OF_RANGE, note
from ownvitals.ingest.when import from_epoch, in_range, zone_name
from ownvitals.withings.models import IntradayBody, SleepBody

_SLEEP_FIELDS = (
    "hr",
    "rr",
    "snoring",
    "sdnn_1",
    "rmssd",
    "hrv_quality",
    "mvt_score",
    "chest_movement_rate",
    "withings_index",
    "breathing_sounds",
)
_INTRA_FIELDS = (
    "steps",
    "elevation",
    "calories",
    "distance",
    "heart_rate",
    "spo2_auto",
    "rr",
    "rmssd",
    "sdnn1",
)
_INSERT_SLEEP = text(
    "INSERT INTO sleep_series (owner_id, connection_id, ts, source_tz, series, value_raw, unit) "
    "VALUES (:owner, :connection, :ts, :zone, :name, :value, :unit) "
    "ON CONFLICT (owner_id, connection_id, series, ts) DO UPDATE SET "
    "value_raw = EXCLUDED.value_raw, unit = EXCLUDED.unit, source_tz = EXCLUDED.source_tz "
    "WHERE sleep_series.value_raw IS DISTINCT FROM EXCLUDED.value_raw "
    "OR sleep_series.unit IS DISTINCT FROM EXCLUDED.unit RETURNING id"
)
_INSERT_INTRA = text(
    "INSERT INTO intraday (owner_id, connection_id, ts, source_tz, metric, value_raw, unit) "
    "VALUES (:owner, :connection, :ts, :zone, :name, :value, :unit) "
    "ON CONFLICT (owner_id, connection_id, metric, ts) DO UPDATE SET "
    "value_raw = EXCLUDED.value_raw, unit = EXCLUDED.unit, source_tz = EXCLUDED.source_tz "
    "WHERE intraday.value_raw IS DISTINCT FROM EXCLUDED.value_raw "
    "OR intraday.unit IS DISTINCT FROM EXCLUDED.unit RETURNING id"
)


@dataclass(frozen=True, slots=True)
class Point:
    ts: datetime
    zone: str
    name: str
    value: int
    unit: int


def _point(
    stream: str, epoch: int, value: Decimal, zone: str, name: str, now: datetime
) -> Point | None:
    instant = from_epoch(epoch)
    if not in_range(instant, now):
        note(stream, OUT_OF_RANGE)
        return None
    try:
        raw, unit = fixed_point(value)
    except (ValueError, ArithmeticError):
        note(stream, INVALID)
        return None
    return Point(instant, zone, name, raw, unit)


def sleep_points(body: SleepBody, zone: str, now: datetime) -> list[Point]:
    named = zone_name(None, zone)
    points: list[Point] = []
    for segment in body.series:
        for field in _SLEEP_FIELDS:
            series = getattr(segment, field)
            if not series:
                continue
            for epoch, value in series.items():
                point = _point("sleep_series", int(epoch), value, named, field, now)
                if point is not None:
                    points.append(point)
    return points


def intraday_points(body: IntradayBody, zone: str, now: datetime) -> list[Point]:
    named = zone_name(None, zone)
    points: list[Point] = []
    for epoch, sample in body.series.items():
        for field in _INTRA_FIELDS:
            value = getattr(sample, field)
            if value is None:
                continue
            point = _point("intraday", int(epoch), value, named, field, now)
            if point is not None:
                points.append(point)
    return points


async def write_points(
    db: AsyncSession, table: str, owner: UUID, connection: UUID, points: list[Point]
) -> int:
    """Write points that already have a partition. Returns how many changed."""
    statement = _INSERT_SLEEP if table == "sleep_series" else _INSERT_INTRA
    changed = 0
    for point in points:
        found = (
            await db.execute(
                statement,
                {
                    "owner": owner,
                    "connection": connection,
                    "ts": point.ts,
                    "zone": point.zone,
                    "name": point.name,
                    "value": point.value,
                    "unit": point.unit,
                },
            )
        ).first()
        if found is None:
            continue
        await bump(db, owner, table)
        changed += 1
    return changed
