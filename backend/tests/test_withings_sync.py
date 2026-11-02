"""Backfill resume and capability against the fake. Synthetic measurements only."""

import asyncio
import json
import urllib.request
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ctx import make_context
from jobtools import engine_for, ensure_user
from ownvitals import egress
from ownvitals.config import Settings
from ownvitals.db import create_engine, create_session_factory
from ownvitals.egress import core
from ownvitals.ingest.calls import Account, Halt
from ownvitals.ingest.revisions import current
from ownvitals.jobs.errors import RunStopped
from ownvitals.jobs.fence import Lease
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import StreamInput
from ownvitals.withings.capabilities import shown_count
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.sync import execute_backfill
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingsschema import new_app


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
