"""Reconciliation, deletions and subscription drift against the fake. Synthetic only."""

import asyncio
import json
import logging
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
from ownvitals.db import create_engine, create_session_factory, user_session
from ownvitals.egress import core
from ownvitals.ingest.calls import Account
from ownvitals.ingest.revisions import current
from ownvitals.jobs import KINDS
from ownvitals.jobs.errors import JobFailure
from ownvitals.jobs.fence import Lease
from ownvitals.jobs.run import JobRun
from ownvitals.jobs.withings import ConnectInput
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.reconcile import (
    execute_reconcile,
    marked_for_resubscribe,
    repair_subscriptions,
)
from ownvitals.withings.subscriptions import callback_url
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingsschema import new_app
from withingsstore import delete_group, edit_group, find_group
from withingswire import Subscription

BASE = "https://hooks.example.test"
CURRENT = callback_url(BASE, "current-marker")
STALE = callback_url(BASE, "stale-marker")
EXTRA = callback_url(BASE, "extra-marker")
FOREIGN = "https://other.example.test/api/webhooks/withings/foreign-marker"
PLANTED = 8_675_309


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


def test_import_leaves_the_kind_registry_empty() -> None:
    assert KINDS.names() == []


def test_only_a_boolean_mark_means_resubscribe() -> None:
    assert marked_for_resubscribe({"resubscribe": True}) is True
    assert marked_for_resubscribe({"resubscribe": "true"}) is False
    assert marked_for_resubscribe({}) is False


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
                "VALUES (:id, :owner, 'withings_reconcile', 'running', CAST(:input AS jsonb), "
                "'scheduler', 4, 'synthetic-worker', now() + interval '30 minutes', now(), 1, 1)"
            ),
            {"id": job_id, "owner": owner, "input": json.dumps({"connection_id": str(connection)})},
        )
    return owner, connection, job_id


def _run(
    factory: async_sessionmaker[AsyncSession],
    owner: uuid.UUID,
    connection: uuid.UUID,
    job_id: uuid.UUID,
) -> JobRun:
    return JobRun(
        factory,
        Lease(job_id, 1, make_context(owner)),
        kind="withings_reconcile",
        input=ConnectInput(connection_id=connection),
        attempt=1,
        lease_seconds=120,
        shutdown=asyncio.Event(),
    )


def _refill(database_url: str, app_id: int) -> None:
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_rate_buckets SET remaining = budget, "
                "refilled_at = clock_timestamp(), paused_until = NULL WHERE app_id = :app"
            ),
            {"app": app_id},
        )


def _copy_group(
    database_url: str,
    source: uuid.UUID,
    owner: uuid.UUID,
    connection: uuid.UUID,
    grpid: int,
    *,
    status: str = "needs_reconnect",
) -> None:
    with engine_for(database_url) as engine, engine.begin() as conn:
        ensure_user(conn, owner)
        conn.execute(
            text(
                "INSERT INTO connections (id, owner_id, provider, status, public) "
                "VALUES (:id, :owner, 'withings', :status, '{}'::jsonb)"
            ),
            {"id": connection, "owner": owner, "status": status},
        )
        conn.execute(
            text(
                "INSERT INTO measure_groups (owner_id, connection_id, grpid, measured_at, "
                "source_tz, attrib, category, model_id, hash_deviceid, created_at, modified_at, "
                "raw, payload_hash) SELECT :owner, :connection, grpid, measured_at, source_tz, "
                "attrib, category, model_id, hash_deviceid, created_at, modified_at, raw, "
                "payload_hash FROM measure_groups WHERE owner_id = :source AND grpid = :grpid"
            ),
            {"owner": owner, "connection": connection, "source": source, "grpid": grpid},
        )


async def _deleted(
    factory: async_sessionmaker[AsyncSession], owner: uuid.UUID, connection: uuid.UUID, grpid: int
) -> bool:
    async with user_session(factory, make_context(owner)) as db:
        flag = (
            await db.execute(
                text(
                    "SELECT deleted_at IS NOT NULL FROM measure_groups "
                    "WHERE owner_id = :owner AND connection_id = :connection AND grpid = :grpid"
                ),
                {"owner": owner, "connection": connection, "grpid": grpid},
            )
        ).scalar_one()
    return bool(flag)


async def _absent(factory: async_sessionmaker[AsyncSession], owner: uuid.UUID) -> list[str]:
    async with user_session(factory, make_context(owner)) as db:
        state = (
            await db.execute(
                text(
                    "SELECT backfill_state FROM sync_cursors "
                    "WHERE owner_id = :owner AND stream = 'measures'"
                ),
                {"owner": owner},
            )
        ).scalar_one()
    raw = state.get("reconcile_absent") if isinstance(state, dict) else None
    return [] if not isinstance(raw, list) else [str(item) for item in raw]


async def _revision(factory: async_sessionmaker[AsyncSession], owner: uuid.UUID) -> int:
    async with user_session(factory, make_context(owner)) as db:
        return await current(db, owner, "measures")


async def test_a_row_is_deleted_only_after_two_complete_passes(
    sessions: async_sessionmaker[AsyncSession],
    database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing once, or missing on an errored pass, stays. Two complete misses delete it."""
    caplog.set_level(logging.DEBUG)
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    other_owner, other_connection = uuid.uuid4(), uuid.uuid4()
    fake = FakeWithings(seed=26, rate_limit=None)
    app = fake.add_app()
    user = fake.add_user(days=5, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.metrics",), "UTC", token)
    job = _run(sessions, owner, connection, job_id)

    async def once() -> object:
        _refill(database_url, app_id)
        async with WithingsClient.open(
            WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
        ) as client:
            return await execute_reconcile(job, account, client, "measures")

    first = await once()
    assert first.complete is True  # pyright: ignore[reportAttributeAccessIssue]
    async with user_session(sessions, make_context(owner)) as db:
        grpid = int(
            (
                await db.execute(
                    text("SELECT grpid FROM measure_groups WHERE owner_id = :owner LIMIT 1"),
                    {"owner": owner},
                )
            ).scalar_one()
        )
    _copy_group(database_url, owner, other_owner, other_connection, grpid)
    same_owner_connection = uuid.uuid4()
    _copy_group(database_url, owner, owner, same_owner_connection, grpid, status="disconnected")
    revision = await _revision(sessions, owner)
    delete_group(user.data, grpid)

    nxt = fake.endpoint_counts.get("measure:getmeas", 0) + 1
    fake.inject("status", at=nxt, endpoint="measure:getmeas", status=2554)
    unsupported = await once()
    assert unsupported.complete is False  # pyright: ignore[reportAttributeAccessIssue]
    assert await _deleted(sessions, owner, connection, grpid) is False
    assert await _absent(sessions, owner) == []
    fake.failures.clear()

    missing_once = await once()
    assert missing_once.complete is True  # pyright: ignore[reportAttributeAccessIssue]
    assert missing_once.deleted == 0  # pyright: ignore[reportAttributeAccessIssue]
    assert await _deleted(sessions, owner, connection, grpid) is False
    assert await _absent(sessions, owner) == [str(grpid)]
    assert await _revision(sessions, owner) == revision

    nxt = fake.endpoint_counts.get("measure:getmeas", 0) + 1
    fake.inject("status", at=nxt, endpoint="measure:getmeas", status=503)
    with pytest.raises(JobFailure) as raised:
        await once()
    assert raised.value.category == "invalid_input"
    assert await _deleted(sessions, owner, connection, grpid) is False
    assert await _absent(sessions, owner) == [str(grpid)]
    fake.failures.clear()

    deleted = await once()
    assert deleted.deleted == 1  # pyright: ignore[reportAttributeAccessIssue]
    assert await _deleted(sessions, owner, connection, grpid) is True
    assert str(grpid) not in await _absent(sessions, owner)
    assert await _revision(sessions, owner) == revision + 1
    assert await _deleted(sessions, other_owner, other_connection, grpid) is False
    async with user_session(sessions, make_context(owner)) as db:
        kept = (
            await db.execute(
                text(
                    "SELECT deleted_at IS NOT NULL FROM measure_groups "
                    "WHERE owner_id = :owner AND connection_id = :connection AND grpid = :grpid"
                ),
                {"owner": owner, "connection": same_owner_connection, "grpid": grpid},
            )
        ).scalar_one()
    assert kept is False
    assert "stale-marker" not in caplog.text


async def test_a_backdated_edit_updates_the_row_and_bumps_the_revision(
    sessions: async_sessionmaker[AsyncSession], database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=27, rate_limit=None)
    app = fake.add_app()
    user = fake.add_user(days=4, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.metrics",), "UTC", token)
    job = _run(sessions, owner, connection, job_id)

    async def once() -> object:
        _refill(database_url, app_id)
        async with WithingsClient.open(
            WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
        ) as client:
            return await execute_reconcile(job, account, client, "measures")

    await once()
    async with user_session(sessions, make_context(owner)) as db:
        grpid = int(
            (
                await db.execute(
                    text("SELECT grpid FROM measure_groups WHERE owner_id = :owner LIMIT 1"),
                    {"owner": owner},
                )
            ).scalar_one()
        )
    held = await _revision(sessions, owner)
    unchanged = await once()
    assert unchanged.changed == 0  # pyright: ignore[reportAttributeAccessIssue]
    assert await _revision(sessions, owner) == held
    group = find_group(user.data, grpid)
    kind = int(group["measures"][0]["type"])
    edit_group(
        user.data,
        grpid,
        now=int(datetime.now(UTC).timestamp()),
        measures=[{**group["measures"][0], "value": PLANTED}],
    )
    edited = await once()
    assert edited.changed == 1  # pyright: ignore[reportAttributeAccessIssue]
    assert await _revision(sessions, owner) == held + 1
    async with user_session(sessions, make_context(owner)) as db:
        stored = (
            await db.execute(
                text(
                    "SELECT m.value_raw FROM measures m JOIN measure_groups g "
                    "ON g.owner_id = m.owner_id AND g.id = m.measure_group_id "
                    "WHERE g.owner_id = :owner AND g.grpid = :grpid AND m.type = :type"
                ),
                {"owner": owner, "grpid": grpid, "type": kind},
            )
        ).scalar_one()
        state = (
            await db.execute(
                text("SELECT backfill_state::text FROM sync_cursors WHERE owner_id = :owner"),
                {"owner": owner},
            )
        ).scalar_one()
    assert int(stored) == PLANTED
    assert str(PLANTED) not in state
    assert str(PLANTED) not in caplog.text


async def test_subscription_drift_is_repaired_without_logging_urls(
    sessions: async_sessionmaker[AsyncSession],
    database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=28, rate_limit=None)
    app = fake.add_app()
    user = fake.add_user(days=1, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    other = fake.add_user(days=1, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.metrics",), "UTC", token)
    fake.subscriptions.extend(
        (
            Subscription(app.client_id, user.userid, 1, STALE),
            Subscription(app.client_id, user.userid, 1, EXTRA),
            Subscription(app.client_id, user.userid, 2, CURRENT),
            Subscription(app.client_id, user.userid, 16, FOREIGN),
            Subscription(app.client_id, other.userid, 1, STALE),
        )
    )
    job = _run(sessions, owner, connection, job_id)
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
    ) as client:
        repair = await repair_subscriptions(
            job,
            account,
            client,
            callback_base=BASE,
            current_url=CURRENT,
            resubscribe=False,
            applis=(1, 2, 4),
        )
    assert (repair.revoked, repair.updated, repair.subscribed) == (1, 1, 1)
    mine = [item for item in fake.subscriptions if item.userid == user.userid]
    assert sorted((item.appli, item.callbackurl) for item in mine) == [
        (1, CURRENT),
        (2, CURRENT),
        (4, CURRENT),
        (16, FOREIGN),
    ]
    assert [item.callbackurl for item in fake.subscriptions if item.userid == other.userid] == [
        STALE
    ]
    assert "stale-marker" not in caplog.text
    assert "current-marker" not in caplog.text


async def test_a_marked_connection_is_resubscribed_after_cleanup(
    sessions: async_sessionmaker[AsyncSession], database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app_id = new_app(database_url)
    owner, connection, job_id = _plant(database_url, app_id)
    fake = FakeWithings(seed=29, rate_limit=None)
    app = fake.add_app()
    user = fake.add_user(days=1, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    token = fake.tokens_for(app.client_id, user.userid)["access_token"]
    account = Account(connection, app_id, ("user.metrics",), "UTC", token)
    fake.subscriptions.append(Subscription(app.client_id, user.userid, 1, STALE))
    job = _run(sessions, owner, connection, job_id)
    start = len(fake.log)
    assert marked_for_resubscribe({"resubscribe": True}) is True
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
    ) as client:
        repair = await repair_subscriptions(
            job,
            account,
            client,
            callback_base=BASE,
            current_url=CURRENT,
            resubscribe=True,
            applis=(1, 4),
        )
    assert repair.updated == 0
    assert repair.revoked == 1
    assert repair.subscribed == 2
    keys = [entry.endpoint for entry in fake.log[start:]]
    assert "notify:update" not in keys
    assert sorted((item.appli, item.callbackurl) for item in fake.subscriptions) == [
        (1, CURRENT),
        (4, CURRENT),
    ]
    assert "stale-marker" not in caplog.text
