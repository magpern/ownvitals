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
from sqlalchemy.ext.asyncio import AsyncEngine

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import Factory, superuser
from ctx import make_context
from jobcore import settings_for
from jobtools import ensure_user, new_job
from ownvitals.db import create_session_factory, system_session, user_session
from ownvitals.db.queue_check import verify_queue
from ownvitals.jobs import queue
from ownvitals.metrics import REGISTRY
from ownvitals.withings import metrics as wm
from ownvitals.worker import metrics as worker_metrics
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
AGES = text("SELECT * FROM public.metrics_webhook_ages(:after, :after_id)")
START = uuid.UUID(int=0)
BURST = text(
    "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, enddate, "
    "dedupe_key, received_at) SELECT :app, 7310100, 1, :end - g, :end - g, "
    "'synthetic-burst-' || g, CASE WHEN g <= :tied THEN :t ELSE :t + interval '1 second' END "
    "FROM generate_series(1, :n) AS g"
)


@pytest.fixture
def clean_withings(database_url: str) -> None:
    with superuser(database_url) as conn:
        conn.execute(text("TRUNCATE webhook_events, withings_apps CASCADE"))


@pytest.fixture
async def engine(app_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_worker_engine(settings_for(app_database_url), pool_size=4)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def factory(engine: AsyncEngine) -> Factory:
    return create_session_factory(engine)


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
        day_ago = datetime.now(UTC) - timedelta(days=1)
        ages = (await db.execute(AGES, {"after": day_ago, "after_id": START})).all()
    assert rows
    assert ages
    flat = {str(v) for row in [*rows, *ages] for v in row}
    assert not flat & ids
    assert list(ages[0]._fields) == ["received", "event_id", "age_seconds"]  # a cursor, no owner
    assert {r.metric for r in rows} == {
        "last_sync_age",
        "subscriptions",
        "completeness",
        "queue_oldest_age",
    }
    owner = uuid.UUID(next(iter(ids)))
    now = datetime.now(UTC)
    for statement, params in ((AGGREGATES, {}), (AGES, {"after": now, "after_id": START})):
        with pytest.raises(DBAPIError, match="system_session_only"):
            async with user_session(factory, make_context(owner)) as db:
                await db.execute(statement, params)
    for bad in ({"after": None, "after_id": START}, {"after": now, "after_id": None}):
        with pytest.raises(DBAPIError, match="bad_after"):
            async with system_session(factory) as db:
                await db.execute(AGES, bad)
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
    engine: AsyncEngine, factory: Factory, app_database_url: str, database_url: str
) -> None:
    app_id = _seed(database_url)
    port = _free_port()
    settings = settings_for(
        app_database_url, metrics_bind="127.0.0.1", metrics_port=port, metrics_refresh_seconds=5
    )
    stop = asyncio.Event()
    wm.subscriptions.reset()
    await verify_queue(engine)  # the refresh waits for the worker's queue check (#303)
    async with metrics_running(settings, factory, stop, engine=engine) as refresher:
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


def _burst(url: str, received: datetime, *, n: int, tied: int) -> list[float]:
    """``n`` synthetic notifications: the first ``tied`` share one received time (as one
    transaction's ``now()`` would), the rest one second later; notification g is g seconds old
    (plus that second). Returns the ages, sorted."""
    app_id = new_app(url)
    end = int(received.timestamp())
    with superuser(url) as conn:
        conn.execute(BURST, {"app": app_id, "end": end, "t": received, "tied": tied, "n": n})
    return sorted(float(g if g <= tied else g + 1) for g in range(1, n + 1))


@pytest.fixture
def observed(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Each delivery age the refresh records, in order."""
    ages: list[float] = []
    monkeypatch.setattr(worker_metrics, "record_webhook_age", ages.append)
    return ages


def _start() -> datetime:
    return datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)


@pytest.mark.usefixtures("clean_withings")
async def test_a_burst_over_the_page_with_equal_times_is_counted_exactly_once(
    factory: Factory, database_url: str, observed: list[float]
) -> None:
    """#303: 2500 notifications, 1200 of them received at the same instant, so the first page
    ends inside equal times. One refresh reads them all, each once; the next reads none."""
    start = _start()
    expected = _burst(database_url, start + timedelta(seconds=10), n=2500, tied=1200)
    refresher = Refresher(factory, since=start)
    await refresher.refresh()
    assert sorted(observed) == expected  # every row once: none skipped, none twice
    await refresher.refresh()
    assert len(observed) == 2500


@pytest.mark.usefixtures("clean_withings")
async def test_a_backlog_over_the_pages_continues_on_the_next_refresh(
    monkeypatch: pytest.MonkeyPatch, factory: Factory, database_url: str, observed: list[float]
) -> None:
    """One page per refresh: 2500 rows take three refreshes of 1000, 1000 and 500, the first
    page boundary inside 1200 equal received times, and a fourth finds nothing."""
    monkeypatch.setattr(worker_metrics, "AGES_PAGES", 1)
    start = _start()
    expected = _burst(database_url, start + timedelta(seconds=10), n=2500, tied=1200)
    refresher = Refresher(factory, since=start)
    seen: list[int] = []
    for _ in range(4):
        await refresher.refresh()
        seen.append(len(observed))
    assert seen == [1000, 2000, 2500, 2500]
    assert sorted(observed) == expected


@pytest.mark.usefixtures("clean_withings")
async def test_rows_at_the_start_time_are_counted(
    factory: Factory, database_url: str, observed: list[float]
) -> None:
    """The watermark starts at (start time, the lowest id): rows received at that very instant
    are after it."""
    start = _start()
    expected = _burst(database_url, start, n=3, tied=3)
    await Refresher(factory, since=start).refresh()
    assert sorted(observed) == expected


@pytest.mark.usefixtures("clean_withings")
async def test_a_failed_read_keeps_the_watermark(
    monkeypatch: pytest.MonkeyPatch, factory: Factory, database_url: str, observed: list[float]
) -> None:
    """A refresh that fails after reading records nothing and does not move the watermark: the
    next one counts the same rows, once."""
    start = _start()
    expected = _burst(database_url, start + timedelta(seconds=10), n=1500, tied=1500)
    refresher = Refresher(factory, since=start)

    async def broken(_factory: Factory) -> list[tuple[str, int]]:
        raise RuntimeError("synthetic: queue_stats failed")

    with monkeypatch.context() as patch:
        patch.setattr(worker_metrics.queue, "stats", broken)
        with pytest.raises(RuntimeError):
            await refresher.refresh()
    assert observed == []
    assert (refresher.after, refresher.after_id) == (start, START)
    await refresher.refresh()
    assert sorted(observed) == expected
