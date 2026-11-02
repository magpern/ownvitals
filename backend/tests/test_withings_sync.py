"""Backfill resume and capability against the fake. Synthetic measurements only."""

import asyncio
import json
import urllib.request
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ctx import make_context
from jobtools import engine_for, ensure_user
from ownvitals import egress
from ownvitals.config import Settings
from ownvitals.db import create_engine, create_session_factory, user_session
from ownvitals.egress import core
from ownvitals.ingest.calls import Account, Halt
from ownvitals.ingest.cursors import write_cursor
from ownvitals.ingest.revisions import current
from ownvitals.jobs.errors import RunStopped
from ownvitals.jobs.fence import Lease
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import StreamInput
from ownvitals.withings.capabilities import shown_count
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.sync import execute_backfill, execute_sync
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingsschema import new_app
from withingsstreams import START


@pytest.fixture(autouse=True)
def _worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=False)


@pytest.fixture
async def sessions(
    app_database_url: str, schema: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _plant(database_url: str, app_id: int) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner, connection, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as conn:
        ensure_user(conn, owner)
        conn.execute(
            text(
                "INSERT INTO connections (id, owner_id, provider, status, public) "
                "VALUES (:id, :owner, 'withings', 'needs_reconnect', CAST(:public AS jsonb))"
            ),
            {
                "id": connection,
                "owner": owner,
                "public": json.dumps({"app_id": app_id, "scopes": ["user.metrics"]}),
            },
        )
        conn.execute(
            text(
                "INSERT INTO jobs (id, owner_id, kind, state, input, origin, lane, leased_by, "
                "lease_expires_at, heartbeat_at, lease_version, attempt) "
                "VALUES (:id, :owner, 'withings_backfill', 'running', CAST(:input AS jsonb), "
                "'scheduler', 5, 'synthetic-worker', now() + interval '30 minutes', now(), 1, 1)"
            ),
            {
                "id": job_id,
                "owner": owner,
                "input": json.dumps({"connection_id": str(connection), "stream": "measures"}),
            },
        )
    return owner, connection, job_id


def _run(
    factory: async_sessionmaker[AsyncSession],
    owner: uuid.UUID,
    connection: uuid.UUID,
    job_id: uuid.UUID,
) -> JobRun:
    ctx = make_context(owner)
    return JobRun(
        factory,
        Lease(job_id, 1, ctx),
        kind="withings_backfill",
        input=StreamInput(connection_id=connection, stream="measures"),
        attempt=1,
        lease_seconds=120,
        shutdown=asyncio.Event(),
    )


async def _rows(factory: async_sessionmaker[AsyncSession], owner: uuid.UUID) -> int:
    from ownvitals.db import user_session

    async with user_session(factory, make_context(owner)) as db:
        count = (
            await db.execute(
                text("SELECT count(*) FROM measure_groups WHERE owner_id = :owner"),
                {"owner": owner},
            )
        ).scalar_one()
        distinct = (
            await db.execute(
                text("SELECT count(DISTINCT grpid) FROM measure_groups WHERE owner_id = :owner"),
                {"owner": owner},
            )
        ).scalar_one()
    assert count == distinct
    return int(count)


async def test_backfill_resumes_after_a_lost_lease_and_a_rerun_ingests_nothing(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=25)
    app = fake.add_app()
    user = fake.add_user(days=800, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.metrics",), "UTC", token)
    expected = {
        group["grpid"] for group in user.data.measuregrps if group.get("category") in (1, 2)
    }
    job = _run(sessions, owner, connection, job_id)
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
    ) as client:
        with pytest.raises(RunStopped) as stopped:
            await execute_backfill(job, account, client, "measures", halt=Halt(after_windows=16))
        assert stopped.value.reason == "lost"
        partial = await _rows(sessions, owner)
        assert partial > 0
        capability = await execute_backfill(job, account, client, "measures")
        done = await _rows(sessions, owner)
        from ownvitals.db import user_session

        async with user_session(sessions, make_context(owner)) as db:
            stored = {
                row
                for row in (
                    await db.execute(
                        text("SELECT grpid FROM measure_groups WHERE owner_id = :owner"),
                        {"owner": owner},
                    )
                ).scalars()
            }
            revision = await current(db, owner, "measures")
            cursor = (
                await db.execute(
                    text(
                        "SELECT capability, backfill_state FROM sync_cursors "
                        "WHERE owner_id = :owner AND stream = 'measures'"
                    ),
                    {"owner": owner},
                )
            ).one()
        assert capability == "supported"
        assert done > partial
        assert stored == expected
        assert cursor.capability == "supported"
        assert cursor.backfill_state["status"] == "complete"
        await execute_backfill(job, account, client, "measures")
        assert await _rows(sessions, owner) == done
        async with user_session(sessions, make_context(owner)) as db:
            assert await current(db, owner, "measures") == revision
    assert fake.request_count < 120


async def test_an_unsupported_stream_is_not_stored_as_zero(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=4)
    app = fake.add_app()
    user = fake.add_user(days=3, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    fake.inject("status", at=1, endpoint="measure:getmeas", status=2554)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.metrics",), "UTC", token)
    job = _run(sessions, owner, connection, job_id)
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
    ) as client:
        capability = await execute_backfill(job, account, client, "measures")
    assert capability == "unsupported"
    assert shown_count(capability, 0) is None
    assert await _rows(sessions, owner) == 0


async def test_a_missing_scope_is_not_authorized_and_makes_no_call(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=5)
    app = fake.add_app()
    user = fake.add_user(days=3, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    token = fake.tokens_for(app.client_id, user.userid, scope="user.info")["access_token"]
    account = Account(connection, app_id, ("user.info",), "UTC", token)
    before = fake.request_count
    job = _run(sessions, owner, connection, job_id)
    moment = datetime(2026, 11, 2, tzinfo=UTC)
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
    ) as client:
        capability = await execute_backfill(job, account, client, "measures", now=moment)
    assert capability == "not_authorized"
    assert shown_count(capability, 0) is None
    assert fake.request_count == before
    assert await _rows(sessions, owner) == 0


def _number(value: object) -> Decimal:
    return Decimal(str(value))


async def _mark_complete(
    factory: async_sessionmaker[AsyncSession],
    owner: uuid.UUID,
    connection: uuid.UUID,
    stream: str,
    lastupdate: int | None,
) -> None:
    async with user_session(factory, make_context(owner)) as db:
        await write_cursor(
            db,
            owner,
            connection,
            stream,
            capability="supported",
            state={"status": "complete", "index": 1, "skipped_out_of_range": 0},
            lastupdate=lastupdate,
            zone="UTC",
        )


async def test_a_change_older_than_a_day_is_fetched_by_lastupdate(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=25)
    app = fake.add_app()
    user = fake.add_user(days=5, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.activity",), "UTC", token)
    await _mark_complete(sessions, owner, connection, "activity_daily", None)
    moment = datetime.fromtimestamp(START, UTC)
    job = _run(sessions, owner, connection, job_id)
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
    ) as client:
        await execute_sync(job, account, client, "activity_daily", now=moment)
        old = user.data.activities[0]
        changed = datetime.fromtimestamp(START + 3 * 86_400, UTC)
        assert (changed.date() - date.fromisoformat(old["date"])).days > 1
        old["steps"] = 400
        old["elevation"] = 12345
        old["modified"] = int(changed.timestamp())
        await execute_sync(job, account, client, "activity_daily", now=changed)
    calls = [entry for entry in fake.log if entry.endpoint == "v2/measure:getactivity"]
    assert calls[-1].params["lastupdate"] == str(START)
    assert "startdateymd" not in calls[-1].params
    async with user_session(sessions, make_context(owner)) as db:
        raw = (
            await db.execute(
                text(
                    "SELECT raw FROM activity_daily WHERE owner_id = :owner AND day = :day"
                ),
                {"owner": owner, "day": date.fromisoformat(old["date"])},
            )
        ).scalar_one()
        cursor = (
            await db.execute(
                text(
                    "SELECT lastupdate FROM sync_cursors "
                    "WHERE owner_id = :owner AND stream = 'activity_daily'"
                ),
                {"owner": owner},
            )
        ).scalar_one()
    assert _number(raw["fields"]["steps"]) == 400
    assert _number(raw["fields"]["elevation"]) == 12345
    assert cursor == int(changed.timestamp())


async def test_an_outage_longer_than_a_day_fetches_the_missed_sample(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=8)
    app = fake.add_app()
    user = fake.add_user(days=1, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    sample = START + 86_400
    user.data.intraday = {
        sample: {
            "steps": 7,
            "duration": 15,
            "stroke": 3,
            "pool_lap": 2,
            "hrv_quality": 1,
        }
    }
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.activity",), "UTC", token)
    await _mark_complete(sessions, owner, connection, "intraday", START)
    moment = datetime.fromtimestamp(START + 3 * 86_400, UTC)
    job = _run(sessions, owner, connection, job_id)
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
    ) as client:
        await execute_sync(job, account, client, "intraday", now=moment)
    async with user_session(sessions, make_context(owner)) as db:
        stored = {
            row.metric: row.value_raw
            for row in (
                await db.execute(
                    text("SELECT metric, value_raw FROM intraday WHERE owner_id = :owner"),
                    {"owner": owner},
                )
            )
        }
    assert stored["steps"] == 7
    assert stored["duration"] == 15
    assert stored["stroke"] == 3
    assert stored["pool_lap"] == 2
    assert stored["hrv_quality"] == 1


async def test_out_of_range_series_and_devices_move_the_skip_count(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=9)
    app = fake.add_app()
    user = fake.add_user(days=1, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    user.data.sleep_series = [
        {
            "model": 63,
            "series": [
                {
                    "startdate": 1_200_000_000,
                    "enddate": 1_230_768_000 + 3600,
                    "state": 3,
                    "hr": {"1200000000": 61},
                }
            ],
        }
    ]
    user.data.devices = [
        {
            "deviceid": "synthetic-device",
            "type": "scale",
            "model": "Body",
            "model_id": 1,
            "battery": "high",
            "hash_deviceid": "synthetic-device",
            "timezone": "UTC",
            "last_session_date": 1_200_000_000,
        }
    ]
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.activity", "user.info"), "UTC", token)
    job = _run(sessions, owner, connection, job_id)
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
    ) as client:
        await execute_backfill(
            job, account, client, "sleep_series", now=datetime(2009, 1, 2, 12, tzinfo=UTC)
        )
        await execute_backfill(
            job, account, client, "devices", now=datetime(2026, 11, 2, tzinfo=UTC)
        )
    async with user_session(sessions, make_context(owner)) as db:
        rows = (
            await db.execute(
                text(
                    "SELECT stream, backfill_state FROM sync_cursors WHERE owner_id = :owner "
                    "AND stream IN ('sleep_series', 'devices')"
                ),
                {"owner": owner},
            )
        ).all()
    counts = {row.stream: row.backfill_state["skipped_out_of_range"] for row in rows}
    assert counts["sleep_series"] == 1
    assert counts["devices"] == 1
