"""The worker's DB-derived metrics (#167, PLAN section 4.4 "Observability"), on the real definer
aggregates of ``40-withings.sql`` and a running listener. Synthetic accounts and rows only: no
real account, no health value.

The aggregates count across owners in a system session and refuse a user session; the refresh
writes nothing; every gauge moves through ``Refresher.refresh``; a running worker's
``metrics_running`` serves every PLAN section 4.4 metric on ``/metrics``.
"""

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import Factory, superuser
from ctx import make_context
from jobcore import settings_for
from jobtools import ensure_user, new_job
from ownvitals.db import create_session_factory, system_session, user_session
from ownvitals.jobs import queue
from ownvitals.metrics import REGISTRY
from ownvitals.withings import metrics as wm
from ownvitals.worker.loop import create_worker_engine
from ownvitals.worker.metrics import Refresher, metrics_running
from withingsschema import new_app

pytestmark = pytest.mark.usefixtures("clean_auth")
CONNECT = text(
    "INSERT INTO connections (owner_id, provider, status, public, secret, secret_nonce, "
    "wrapped_dek, dek_nonce, key_id) VALUES (:owner, 'withings', :status, "
    "CAST(:public AS jsonb), :secret, :nonce, :dek, :nonce, :key) RETURNING id"
)
CURSOR = text(
    "INSERT INTO sync_cursors (owner_id, connection_id, stream, capability, backfill_state, "
    "last_ok_at) VALUES (:owner, :connection, :stream, :capability, CAST(:state AS jsonb), :ok)"
)
EVENT = text(
    "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, enddate, "
    "dedupe_key, received_at) VALUES (:app, :userid, 1, :end, :end, :key, :received)"
)
AGGREGATES = text("SELECT * FROM public.metrics_withings_aggregates()")
AGES = text("SELECT * FROM public.metrics_webhook_ages(:after)")


@pytest.fixture
def clean_withings(database_url: str) -> None:
    with superuser(database_url) as conn:
        conn.execute(text("TRUNCATE webhook_events, withings_apps CASCADE"))


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_worker_engine(settings_for(app_database_url), pool_size=4)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _connection(
    conn: Connection, app_id: int, userid: int, status: str, **public: object
) -> tuple[uuid.UUID, uuid.UUID]:
    owner = uuid.uuid4()
    ensure_user(conn, owner)
    live = status != "disconnected"
    connection = conn.execute(
        CONNECT,
        {
            "owner": owner,
            "status": status,
            "public": json.dumps({"app_id": app_id, "withings_userid": userid, **public}),
            "secret": b"\x01" * 16 if live else None,  # labelled filler, not a credential
            "nonce": b"\x02" * 12 if live else None,
            "dek": b"\x03" * 48 if live else None,
            "key": "kek-synthetic" if live else None,
        },
    ).scalar_one()
    return owner, connection


def _cursor(
    conn: Connection,
    owner: uuid.UUID,
    connection: uuid.UUID,
    stream: str,
    capability: str,
    status: str | None,
    ok_hours: float | None,
) -> None:
    conn.execute(
        CURSOR,
        {
            "owner": owner,
            "connection": connection,
            "stream": stream,
            "capability": capability,
            "state": json.dumps({"status": status} if status else {}),
            "ok": None if ok_hours is None else datetime.now(UTC) - timedelta(hours=ok_hours),
        },
    )


def _seed(url: str) -> int:
    """Five synthetic connections; returns the app id. A: connected, notified, three streams.
    B: connected, marked for resubscribe. C: needs reconnect. D: disconnected. E: connected,
    never notified. Two queued jobs, one due 300 s ago, one not due yet."""
    app_id = new_app(url)
    now = datetime.now(UTC)
    with superuser(url) as conn:
        a, a_conn = _connection(conn, app_id, 7_310_001, "connected")
        b, b_conn = _connection(conn, app_id, 7_310_002, "connected", resubscribe=True)
        c, c_conn = _connection(conn, app_id, 7_310_003, "needs_reconnect")
        d, d_conn = _connection(conn, app_id, 7_310_004, "disconnected")
        _connection(conn, app_id, 7_310_005, "connected")
        _cursor(conn, a, a_conn, "measures", "supported", "complete", 2)
        _cursor(conn, a, a_conn, "sleep_summary", "supported", "running", 0.5)
        _cursor(conn, a, a_conn, "ecg", "not_authorized", "complete", None)
        _cursor(conn, b, b_conn, "measures", "supported", "complete", 10)
        _cursor(conn, b, b_conn, "sleep_summary", "supported", None, None)
        _cursor(conn, c, c_conn, "measures", "supported", "complete", 100)
        _cursor(conn, d, d_conn, "measures", "supported", "complete", 200)
        conn.execute(
            EVENT,
            {
                "app": app_id,
                "userid": 7_310_001,
                "end": int(now.timestamp()) - 120,
                "key": "synthetic-metrics-a",
                "received": now,
            },
        )
        new_job(conn, a, available_at=now - timedelta(seconds=300))
        new_job(conn, b, available_at=now + timedelta(hours=1))
    return app_id


async def _refreshed(factory: Factory) -> Refresher:
    refresher = Refresher(factory, since=datetime.now(UTC) - timedelta(minutes=1))
    await refresher.refresh()
    return refresher


@pytest.mark.usefixtures("clean_withings")
async def test_every_db_gauge_moves_through_the_refresh(
    factory: Factory, database_url: str
) -> None:
    _seed(database_url)
    observed = wm.webhook_delivery_age.count()
    refresher = await _refreshed(factory)
    # Oldest last success per stream over connected connections (C and D are left out).
    measures = wm.last_sync_success.get(stream="measures")
    assert measures is not None
    assert 10 * 3600 - 60 < measures < 10 * 3600 + 60
    sleep = wm.last_sync_success.get(stream="sleep_summary")
    assert sleep is not None
    assert 1800 - 60 < sleep < 1800 + 60
    assert wm.last_sync_success.get(stream="ecg") is None  # never synced: no age series
    health = {s: wm.subscriptions.get(state=s) for s in wm.SUBSCRIPTION_STATES}
    assert health == {"active": 1, "pending": 1, "failed": 1, "missing": 1}
    complete = {s: wm.stream_completeness.get(stream="measures", state=s) for s in ("complete",)}
    assert complete == {"complete": 2}
    assert wm.stream_completeness.get(stream="sleep_summary", state="backfilling") == 1
    assert wm.stream_completeness.get(stream="sleep_summary", state="partial") == 1
    assert wm.stream_completeness.get(stream="ecg", state="not_available") == 1
    assert wm.stream_completeness.get(stream="devices", state="complete") == 0
    oldest = wm.queue_age.get()
    assert oldest is not None
    assert 300 - 30 < oldest < 300 + 60
    assert wm.queue_jobs.get(state="queued") == 2
    assert wm.webhook_delivery_age.count() == observed + 1
    # The watermark moved: the same notification is not observed twice.
    await refresher.refresh()
    assert wm.webhook_delivery_age.count() == observed + 1


@pytest.mark.usefixtures("clean_withings")
async def test_the_aggregates_return_no_id_and_refuse_a_user_session(
    factory: Factory, database_url: str
) -> None:
    _seed(database_url)
    with superuser(database_url) as conn:
        ids = {str(v) for v in conn.execute(text("SELECT id FROM connections")).scalars()}
        ids |= {str(v) for v in conn.execute(text("SELECT owner_id FROM connections")).scalars()}
        before = {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()  # noqa: S608
            for t in ("connections", "sync_cursors", "jobs", "webhook_events")
        }
    async with system_session(factory) as db:
        rows = (await db.execute(AGGREGATES)).all()
        ages = (await db.execute(AGES, {"after": datetime.now(UTC) - timedelta(days=1)})).all()
    assert rows
    assert ages
    flat = {str(v) for row in [*rows, *ages] for v in row}
    assert not flat & ids
    assert {r.metric for r in rows} == {
        "last_sync_age",
        "subscriptions",
        "completeness",
        "queue_oldest_age",
    }
    owner = uuid.UUID(next(iter(ids)))
    for statement, params in ((AGGREGATES, {}), (AGES, {"after": datetime.now(UTC)})):
        with pytest.raises(DBAPIError, match="system_session_only"):
            async with user_session(factory, make_context(owner)) as db:
                await db.execute(statement, params)
    with pytest.raises(DBAPIError, match="bad_after"):
        async with system_session(factory) as db:
            await db.execute(AGES, {"after": None})
    with superuser(database_url) as conn:
        after = {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()  # noqa: S608
            for t in before
        }
    assert after == before


async def test_job_states_match_the_real_queue_stats(factory: Factory, database_url: str) -> None:
    """Pins ``JOB_STATES`` against what ``queue_stats()`` returns, not against the SQL text."""
    with superuser(database_url) as conn:
        new_job(conn, uuid.uuid4())
    stats = await queue.stats(factory)
    assert [m.removeprefix("jobs_") for m, _ in stats] == list(wm.JOB_STATES)
    wm.set_queue(dict(stats))
    assert wm.queue_jobs.get(state="queued") == 1


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _scrape(port: int) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /metrics HTTP/1.1\r\nHost: worker\r\n\r\n")
    await writer.drain()
    body = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    await writer.wait_closed()
    return body.decode()


@pytest.mark.usefixtures("clean_withings")
async def test_a_running_worker_exports_every_observability_item(
    factory: Factory, app_database_url: str, database_url: str
) -> None:
    app_id = _seed(database_url)
    port = _free_port()
    settings = settings_for(
        app_database_url, metrics_bind="127.0.0.1", metrics_port=port, metrics_refresh_seconds=5
    )
    stop = asyncio.Event()
    wm.subscriptions.reset()
    async with metrics_running(settings, factory, stop) as refresher:
        for _ in range(100):
            if wm.subscriptions.get(state="active") == 1:
                break
            await asyncio.sleep(0.05)
        with superuser(database_url) as conn:  # a notification after the worker started
            end = int(datetime.now(UTC).timestamp()) - 30
            conn.execute(
                EVENT,
                {
                    "app": app_id,
                    "userid": 7_310_005,
                    "end": end,
                    "key": "synthetic-metrics-e",
                    "received": datetime.now(UTC),
                },
            )
        observed = wm.webhook_delivery_age.count()
        await refresher.refresh()
        assert wm.webhook_delivery_age.count() == observed + 1
        body = await _scrape(port)
        stop.set()
    assert body.startswith("HTTP/1.1 200 OK\r\n")
    for metric in REGISTRY.metrics:
        assert f"# TYPE {metric.name} {metric.kind}" in body, metric.name
    for series in (
        'ownvitals_withings_last_sync_success_age_seconds{stream="measures"}',
        # E was "missing" until its notification above: now A and E are active.
        'ownvitals_withings_subscriptions{state="active"} 2',
        'ownvitals_withings_subscriptions{state="missing"} 0',
        'ownvitals_withings_subscriptions{state="pending"} 1',
        'ownvitals_withings_subscriptions{state="failed"} 1',
        'ownvitals_withings_stream_completeness{stream="measures",state="complete"} 2',
        'ownvitals_jobs{state="queued"} 2',
        "ownvitals_jobs_oldest_queued_age_seconds ",
        "ownvitals_withings_webhook_delivery_age_seconds_count ",
    ):
        assert series in body, series
    with pytest.raises(ConnectionRefusedError):
        await _scrape(port)  # the listener closed with the worker
