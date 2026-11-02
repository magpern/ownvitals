"""Revision bumps, out-of-range skips and month partitions. Synthetic rows only."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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
    upsert_ecg,
    upsert_measure_group,
)
from ownvitals.ingest.series import Point, write_points
from ownvitals.ingest.skips import SKIPPED
from ownvitals.withings.models import HeartListEntry, HeartSignal, MeasureGroup

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
