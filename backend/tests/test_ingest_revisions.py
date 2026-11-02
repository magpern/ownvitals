"""Revision bumps, out-of-range skips and month partitions. Synthetic rows only."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ctx import make_context
from jobtools import engine_for, ensure_user
from ownvitals.config import Settings
from ownvitals.db import create_engine, create_session_factory, user_session
from ownvitals.ingest.codec import decode_signal, estimate_current
from ownvitals.ingest.partitions import ensure_month
from ownvitals.ingest.revisions import current
from ownvitals.ingest.rows import (
    save_estimated,
    soft_delete_measure,
    upsert_activity,
    upsert_ecg,
    upsert_measure_group,
    upsert_sleep_summary,
    upsert_workout,
)
from ownvitals.ingest.series import Point, intraday_points, sleep_points, write_points
from ownvitals.ingest.skips import SKIPPED
from ownvitals.withings.models import (
    Activity,
    HeartListEntry,
    HeartSignal,
    IntradayBody,
    MeasureGroup,
    SleepBody,
    SleepSummary,
    Workout,
)

WHEN = datetime(2026, 6, 1, tzinfo=UTC)
IN_RANGE = 1_748_736_000  # 2025-06-01 UTC
OUT_OF_RANGE = 1_200_000_000  # 2008, before the 2009 floor


@pytest.fixture
async def sessions(
    app_database_url: str, schema: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _owner(database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    owner, connection = uuid.uuid4(), uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as conn:
        ensure_user(conn, owner)
        conn.execute(
            text(
                "INSERT INTO connections (id, owner_id, provider, status, public) "
                "VALUES (:id, :owner, 'withings', 'needs_reconnect', '{}')"
            ),
            {"id": connection, "owner": owner},
        )
    return owner, connection


def _group(grpid: int, when: int, value: int, meastype: int = 1) -> MeasureGroup:
    return MeasureGroup.model_validate(
        {
            "grpid": grpid,
            "date": when,
            "category": 1,
            "timezone": "UTC",
            "measures": [{"value": value, "type": meastype, "unit": -2}],
        }
    )


async def test_revisions_bump_on_source_changes_but_not_on_estimated_only(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    owner, connection = _owner(database_url)
    ctx = make_context(owner)
    signal = HeartSignal.model_validate({"signal": [1, -2, 3, 4], "sampling_frequency": 300})
    entry = HeartListEntry.model_validate(
        {
            "timestamp": IN_RANGE,
            "ecg": {"signalid": 9, "afib": 0},
            "timezone": "UTC",
            "heart_rate": 60,
        }
    )
    async with user_session(sessions, ctx) as db:
        assert (
            await upsert_measure_group(
                db, owner, connection, _group(1, IN_RANGE, 8050), "UTC", WHEN
            )
            == "inserted"
        )
        assert await current(db, owner, "measures") == 1
        assert (
            await upsert_measure_group(
                db, owner, connection, _group(1, IN_RANGE, 8050), "UTC", WHEN
            )
            == "unchanged"
        )
        assert await current(db, owner, "measures") == 1
        assert (
            await upsert_measure_group(
                db, owner, connection, _group(1, IN_RANGE, 8100), "UTC", WHEN
            )
            == "updated"
        )
        assert await current(db, owner, "measures") == 2
        assert await soft_delete_measure(db, owner, connection, 1) is True
        assert await current(db, owner, "measures") == 3
        assert await soft_delete_measure(db, owner, connection, 1) is False
        assert await current(db, owner, "measures") == 3
        stored = (
            await db.execute(
                text("SELECT type FROM measures WHERE owner_id = :owner"), {"owner": owner}
            )
        ).scalar_one()
        assert stored == 1
        odd = await upsert_measure_group(
            db, owner, connection, _group(2, IN_RANGE, 1, meastype=99999), "UTC", WHEN
        )
        assert odd == "inserted"
        kept = (
            await db.execute(
                text("SELECT type FROM measures WHERE owner_id = :owner AND type = 99999"),
                {"owner": owner},
            )
        ).scalar_one()
        assert kept == 99999
        assert await upsert_ecg(db, owner, connection, entry, signal, "UTC", WHEN) == "inserted"
        before = await current(db, owner, "ecg")
        frame = (
            await db.execute(
                text("SELECT signal FROM ecg_recordings WHERE owner_id = :owner AND signalid = 9"),
                {"owner": owner},
            )
        ).scalar_one()
        assert decode_signal(bytes(frame)) == [[1, -2, 3, 4]]
        await save_estimated(
            db, owner, connection, 9, {"algo_version": "ecg-5.0.0", "note": "derived"}
        )
        assert await current(db, owner, "ecg") == before


async def test_a_null_signal_row_stores_a_current_estimate(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    owner, connection = _owner(database_url)
    ctx = make_context(owner)
    entry = HeartListEntry.model_validate(
        {"timestamp": IN_RANGE, "ecg": {"signalid": 4, "afib": 1}, "timezone": "UTC"}
    )
    async with user_session(sessions, ctx) as db:
        assert await upsert_ecg(db, owner, connection, entry, None, "UTC", WHEN) == "inserted"
        row = (
            await db.execute(
                text(
                    "SELECT signal, estimated FROM ecg_recordings "
                    "WHERE owner_id = :owner AND signalid = 4"
                ),
                {"owner": owner},
            )
        ).one()
    assert row.signal is None
    assert estimate_current(row.estimated, signalid=4, digest=None, sampling_hz=None)
    assert row.estimated["sqi"]["reasons"] == ["signal_missing"]


async def test_out_of_range_rows_are_skipped_and_a_month_is_created_on_its_own(
    sessions: async_sessionmaker[AsyncSession], database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    owner, connection = _owner(database_url)
    ctx = make_context(owner)
    SKIPPED.clear()
    future = int((WHEN + timedelta(days=200)).timestamp())
    async with user_session(sessions, ctx) as db:
        assert (
            await upsert_measure_group(
                db, owner, connection, _group(3, OUT_OF_RANGE, 424242), "UTC", WHEN
            )
            == "skipped"
        )
        assert (
            await upsert_measure_group(
                db, owner, connection, _group(4, future, 424242), "UTC", WHEN
            )
            == "skipped"
        )
        count = (
            await db.execute(
                text("SELECT count(*) FROM measure_groups WHERE owner_id = :owner"),
                {"owner": owner},
            )
        ).scalar_one()
    assert count == 0
    assert SKIPPED["out_of_range"] == 2
    assert "424242" not in caplog.text
    async with user_session(sessions, ctx) as db:
        await ensure_month(db, "sleep_series", datetime(2026, 11, 1).date())
    async with user_session(sessions, ctx) as db:
        changed = await write_points(
            db,
            "sleep_series",
            owner,
            connection,
            [Point(datetime(2026, 11, 2, tzinfo=UTC), "UTC", "hr", 61, 0)],
        )
        stored = (
            await db.execute(
                text("SELECT value_raw FROM sleep_series WHERE owner_id = :owner"), {"owner": owner}
            )
        ).scalar_one()
    assert changed == 1
    assert stored == 61


def _number(value: object) -> Decimal:
    return Decimal(str(value))


async def test_known_measurement_fields_are_stored(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    owner, connection = _owner(database_url)
    ctx = make_context(owner)
    workout = Workout.model_validate(
        {
            "id": 7,
            "category": 1,
            "timezone": "UTC",
            "startdate": IN_RANGE,
            "enddate": IN_RANGE + 600,
            "data": {"steps": 400},
        }
    )
    summary = SleepSummary.model_validate(
        {
            "id": 8,
            "timezone": "UTC",
            "startdate": IN_RANGE,
            "enddate": IN_RANGE + 8 * 3600,
            "date": "2025-06-01",
            "data": {"total_sleep_time": 321},
        }
    )
    activity = Activity.model_validate(
        {
            "date": "2025-06-01",
            "timezone": "UTC",
            "steps": 100,
            "soft": 5,
            "moderate": 6,
            "intense": 7,
            "active": 8,
            "hr_min": 9,
            "hr_max": 10,
            "hr_zone_0": 11,
            "hr_zone_1": 22,
            "hr_zone_2": 33,
            "hr_zone_3": 44,
        }
    )
    sleep = SleepBody.model_validate(
        {
            "series": [
                {
                    "startdate": IN_RANGE,
                    "enddate": IN_RANGE + 60,
                    "state": 3,
                    "hr": {str(IN_RANGE): 61},
                    "rr": {str(IN_RANGE): 12},
                    "snoring": {str(IN_RANGE): 0},
                    "hrv_quality": {str(IN_RANGE): 1},
                }
            ]
        }
    )
    intra = IntradayBody.model_validate(
        {
            "series": {
                str(IN_RANGE): {
                    "steps": 4,
                    "duration": 15,
                    "stroke": 3,
                    "pool_lap": 2,
                    "hrv_quality": 1,
                    "core_body_temperature": "15.5",
                }
            }
        }
    )
    entry = HeartListEntry.model_validate(
        {
            "timestamp": IN_RANGE,
            "timezone": "UTC",
            "ecg": {"signalid": 3, "afib": 0},
            "bloodpressure": {"diastole": 77, "systole": 111},
            "heart_rate": 60,
        }
    )
    signal = HeartSignal.model_validate({"signal": [1, -2, 3, 4], "sampling_frequency": 300})
    async with user_session(sessions, ctx) as db:
        assert await upsert_workout(db, owner, connection, workout, "UTC", WHEN) == "inserted"
        assert (
            await upsert_sleep_summary(db, owner, connection, summary, "UTC", WHEN) == "inserted"
        )
        assert await upsert_activity(db, owner, connection, activity, "UTC", WHEN) == "inserted"
        assert await upsert_ecg(db, owner, connection, entry, signal, "UTC", WHEN) == "inserted"
        workout_raw = (
            await db.execute(
                text("SELECT raw FROM workouts WHERE owner_id = :owner"), {"owner": owner}
            )
        ).scalar_one()
        summary_raw = (
            await db.execute(
                text("SELECT raw FROM sleep_summaries WHERE owner_id = :owner"), {"owner": owner}
            )
        ).scalar_one()
        activity_raw = (
            await db.execute(
                text("SELECT raw FROM activity_daily WHERE owner_id = :owner"), {"owner": owner}
            )
        ).scalar_one()
        reported = (
            await db.execute(
                text(
                    "SELECT reported FROM ecg_recordings WHERE owner_id = :owner AND signalid = 3"
                ),
                {"owner": owner},
            )
        ).scalar_one()
    assert _number(workout_raw["data"]["steps"]) == 400
    assert _number(summary_raw["data"]["total_sleep_time"]) == 321
    fields = activity_raw["fields"]
    assert _number(fields["soft"]) == 5
    assert _number(fields["moderate"]) == 6
    assert _number(fields["intense"]) == 7
    assert _number(fields["active"]) == 8
    assert _number(fields["hr_min"]) == 9
    assert _number(fields["hr_max"]) == 10
    zones = [_number(fields[name]) for name in ("hr_zone_0", "hr_zone_1", "hr_zone_2", "hr_zone_3")]
    assert zones == [Decimal(11), Decimal(22), Decimal(33), Decimal(44)]
    assert reported["bloodpressure"] == {"diastole": 77, "systole": 111}
    points, skipped = sleep_points(sleep, "UTC", WHEN)
    assert skipped == 0
    names = {point.name: point.value for point in points}
    assert names["state"] == 3
    assert names["hr"] == 61
    assert names["rr"] == 12
    assert names["snoring"] == 0
    assert names["hrv_quality"] == 1
    intra_points, intra_skipped = intraday_points(intra, "UTC", WHEN)
    assert intra_skipped == 0
    metrics = {point.name: (point.value, point.unit) for point in intra_points}
    assert metrics["steps"] == (4, 0)
    assert metrics["duration"] == (15, 0)
    assert metrics["stroke"] == (3, 0)
    assert metrics["pool_lap"] == (2, 0)
    assert metrics["hrv_quality"] == (1, 0)
    assert metrics["core_body_temperature"] == (155, -1)
    async with user_session(sessions, ctx) as db:
        await ensure_month(db, "sleep_series", date(2025, 6, 1))
    async with user_session(sessions, ctx) as db:
        await ensure_month(db, "intraday", date(2025, 6, 1))
    async with user_session(sessions, ctx) as db:
        await write_points(db, "sleep_series", owner, connection, points)
        await write_points(db, "intraday", owner, connection, intra_points)
        stored_sleep = {
            row.series: row.value_raw
            for row in (
                await db.execute(
                    text("SELECT series, value_raw FROM sleep_series WHERE owner_id = :owner"),
                    {"owner": owner},
                )
            )
        }
        stored_intra = {
            row.metric: (row.value_raw, row.unit)
            for row in (
                await db.execute(
                    text("SELECT metric, value_raw, unit FROM intraday WHERE owner_id = :owner"),
                    {"owner": owner},
                )
            )
        }
    assert stored_sleep["state"] == 3
    assert stored_sleep["hr"] == 61
    assert stored_sleep["rr"] == 12
    assert stored_intra["duration"] == (15, 0)
    assert stored_intra["stroke"] == (3, 0)
    assert stored_intra["pool_lap"] == (2, 0)
    assert stored_intra["hrv_quality"] == (1, 0)
    assert stored_intra["core_body_temperature"] == (155, -1)


async def test_ecg_metadata_change_bumps_and_an_estimated_only_write_does_not(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    owner, connection = _owner(database_url)
    ctx = make_context(owner)
    signal = HeartSignal.model_validate({"signal": [1, -2, 3, 4], "sampling_frequency": 300})

    def entry(afib: int, signalid: int = 3) -> HeartListEntry:
        return HeartListEntry.model_validate(
            {
                "timestamp": IN_RANGE,
                "timezone": "UTC",
                "heart_rate": 60,
                "ecg": {"signalid": signalid, "afib": afib},
            }
        )

    async with user_session(sessions, ctx) as db:
        assert await upsert_ecg(db, owner, connection, entry(0), signal, "UTC", WHEN) == "inserted"
        assert await current(db, owner, "ecg") == 1
        assert await upsert_ecg(db, owner, connection, entry(0), signal, "UTC", WHEN) == "unchanged"
        assert await current(db, owner, "ecg") == 1
        assert await upsert_ecg(db, owner, connection, entry(1), signal, "UTC", WHEN) == "updated"
        assert await current(db, owner, "ecg") == 2
        stored = (
            await db.execute(
                text("SELECT afib FROM ecg_recordings WHERE owner_id = :owner AND signalid = 3"),
                {"owner": owner},
            )
        ).scalar_one()
        assert stored == 1
        before = await current(db, owner, "ecg")
        await save_estimated(db, owner, connection, 3, {"algo_version": "ecg-5.0.0"})
        assert await current(db, owner, "ecg") == before
        missing = HeartListEntry.model_validate(
            {"timestamp": IN_RANGE, "timezone": "UTC", "ecg": {"signalid": 5, "afib": 0}}
        )
        changed = HeartListEntry.model_validate(
            {"timestamp": IN_RANGE, "timezone": "UTC", "ecg": {"signalid": 5, "afib": 1}}
        )
        assert await upsert_ecg(db, owner, connection, missing, None, "UTC", WHEN) == "inserted"
        assert await upsert_ecg(db, owner, connection, missing, None, "UTC", WHEN) == "unchanged"
        held = await current(db, owner, "ecg")
        assert await upsert_ecg(db, owner, connection, changed, None, "UTC", WHEN) == "updated"
        assert await current(db, owner, "ecg") == held + 1
        afib = (
            await db.execute(
                text("SELECT afib FROM ecg_recordings WHERE owner_id = :owner AND signalid = 5"),
                {"owner": owner},
            )
        ).scalar_one()
        assert afib == 1
