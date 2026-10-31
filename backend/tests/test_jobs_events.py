"""Job progress (ADR 0005 section 7): payload models, ``pg_notify`` on every event insert and the
web hub. The SSE stream and its endpoint are in ``test_jobs_stream``. Synthetic data only."""

import asyncio
import contextlib
import importlib.util
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ctx import make_context
from jobapi import until
from jobcore import (
    JobsEnv,
    claim,
    jenv_fixture,  # noqa: F401 - fixtures
)
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.db import user_session
from ownvitals.jobs import fenced_session, queue
from ownvitals.jobs import hub as hub_module
from ownvitals.jobs import user as job_user
from ownvitals.jobs.events import MODELS, TERMINAL_KINDS, validated_payload
from ownvitals.jobs.fence import Lease
from ownvitals.jobs.hub import EventHub
from ownvitals.jobs.issuer import issue

SECRET = "synthetic-secret-value-7f3a"  # planted to prove it never leaves


def migration_event_kinds() -> set[str]:
    path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "0003_jobs.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {k.strip().strip("'") for k in module.EVENT_KINDS.split(",")}


def test_every_event_kind_has_a_model_and_the_migration_allows_exactly_those() -> None:
    assert set(MODELS) == migration_event_kinds()
    assert {"completed", "failed", "cancelled"} == TERMINAL_KINDS


NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("kind", "payload", "stored"),
    [
        ("queued", None, {}),
        ("queued", {}, {}),
        ("claimed", {"attempt": 1}, {"attempt": 1}),
        ("progress", {"done": 0, "total": 0}, {"done": 0, "total": 0}),
        ("progress", {"done": 3, "total": 7}, {"done": 3, "total": 7}),
        (
            "retry_scheduled",
            {"category": "rate_limited", "available_at": NOW},
            {"category": "rate_limited", "available_at": "2026-10-31T12:00:00Z"},
        ),
        ("failed", {"category": "internal"}, {"category": "internal"}),
        ("cancel_requested", {}, {}),
        ("cancelled", {}, {}),
        ("completed", {}, {}),
        ("requeued", {}, {}),
    ],
)
def test_valid_payloads_are_accepted_and_json_ready(
    kind: str, payload: dict[str, Any] | None, stored: dict[str, Any]
) -> None:
    assert validated_payload(kind, payload) == stored


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (SECRET, {}),
        ("completed", {"note": SECRET}),
        ("claimed", {"attempt": 0}),
        ("claimed", {"attempt": 11}),
        ("claimed", {"attempt": SECRET}),
        ("progress", {"done": 2, "total": 1}),
        ("progress", {"done": 1, "total": 2, "value": SECRET}),
        ("progress", {"done": 1}),
        ("failed", {"category": SECRET}),
        ("failed", {"category": "internal", "detail": SECRET}),
        ("retry_scheduled", {"category": "internal"}),
        ("cancelled", {"x": {"nested": SECRET}}),
    ],
)
def test_other_payloads_are_refused_without_echoing_them(
    kind: str, payload: dict[str, Any]
) -> None:
    with pytest.raises(ValueError, match="event") as refused:
        validated_payload(kind, payload)
    assert SECRET not in str(refused.value)


@pytest.mark.parametrize("stored", [5, "text", [1], True])
def test_a_stored_scalar_or_list_is_no_payload(stored: Any) -> None:
    with pytest.raises(ValueError, match="does not match"):
        validated_payload("progress", stored)


@contextlib.asynccontextmanager
async def listener(database_url: str) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute("LISTEN job_events")
        yield connection


async def drain(connection: psycopg.AsyncConnection[Any]) -> list[str]:
    return [n.payload async for n in connection.notifies(timeout=0.5)]


async def lease_of(env: JobsEnv) -> Lease:
    execution = await claim(env)
    return (await issue(env.factory, execution.claimed, env.accounts)).lease


async def test_every_event_insert_notifies_with_the_job_id_only(
    jenv: JobsEnv, database_url: str
) -> None:
    owner, job_id = jenv.job()
    ctx = make_context(owner)
    async with listener(database_url) as connection:
        lease = await lease_of(jenv)  # the claim function's own 'claimed' event
        assert await drain(connection) == [str(job_id)]
        async with fenced_session(jenv.factory, lease) as fs:  # a worker event
            await fs.event("progress", {"done": 1, "total": 2})
        assert await drain(connection) == [str(job_id)]
        with pytest.raises(RuntimeError):  # noqa: PT012 - a rolled-back event wakes nobody
            async with fenced_session(jenv.factory, lease) as fs:
                await fs.event("progress", {"done": 2, "total": 2})
                raise RuntimeError("abort")
        assert await drain(connection) == []
        async with user_session(jenv.factory, ctx) as db:  # a user event
            assert await job_user.cancel(db, ctx, job_id) == "running"
        assert await drain(connection) == [str(job_id)]
        jenv.expire_lease(job_id)  # the reaper's two loops
        _, poison = jenv.job(owner, attempt=3, max_attempts=3)
        assert await queue.reap(jenv.factory) == 2
        assert sorted(await drain(connection)) == sorted([str(job_id), str(poison)])


async def test_every_event_the_queue_functions_write_matches_its_model(jenv: JobsEnv) -> None:
    owner, claimed = jenv.job()
    _, poison = jenv.job(owner, attempt=3, max_attempts=3)
    past = datetime.now(UTC) - timedelta(minutes=5)
    lapsed: dict[str, Any] = {
        "state": "running",
        "leased_by": "w",
        "lease_expires_at": past,
        "heartbeat_at": past,
        "available_at": datetime.now(UTC) + timedelta(hours=1),  # not claimable below
    }
    _, spent = jenv.job(owner, attempt=3, max_attempts=3, **lapsed)
    _, retried = jenv.job(owner, attempt=1, max_attempts=3, **lapsed)
    _, cancelling = jenv.job(owner, attempt=1, **lapsed)
    jenv.sql("UPDATE jobs SET cancel_requested_at = now() WHERE id = :i", i=cancelling)
    await lease_of(jenv)
    assert await queue.reap(jenv.factory) == 4
    seen: set[str] = set()
    for job_id in (claimed, poison, spent, retried, cancelling):
        for _, kind, payload, _ in jenv.events(job_id):
            validated_payload(kind, payload)  # ValueError if the function wrote anything else
            seen.add(kind)
    assert seen == {"claimed", "failed", "retry_scheduled", "cancelled"}


async def test_a_notification_wakes_only_streams_of_that_job(jenv: JobsEnv) -> None:
    hub = EventHub(jenv.engine)
    one, two = uuid.uuid4(), uuid.uuid4()
    with hub.subscribe(one) as first, hub.subscribe(two) as second:
        hub.notify(str(one))
        hub.notify("not a job id")
        hub.notify("")
        assert first.wake.is_set()
        assert not second.wake.is_set()
        await first.wait(0.01)
        assert not first.wake.is_set()
    hub.notify(str(one))  # nobody is subscribed: nothing happens
    assert hub._subscriptions == {}  # pyright: ignore[reportPrivateUsage]


def kill_listeners(admin: Connection) -> None:
    pids = admin.execute(
        text(
            "SELECT pid FROM pg_stat_activity WHERE query = 'LISTEN job_events' "
            "AND datname = current_database()"
        )
    ).scalars()
    for pid in list(pids):
        admin.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pid})


async def test_the_listener_comes_up_and_reconnects_after_the_connection_dies(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "RECONNECT_MIN_SECONDS", 0.2)
    hub = EventHub(jenv.engine)
    hub.start()
    try:
        await until(lambda: hub.listening)
        with hub.subscribe(uuid.uuid4()) as subscription:
            kill_listeners(jenv.admin)
            await subscription.wait(5)  # woken when the listener goes down
            await until(lambda: hub.listening, 10)  # and it is back after the backoff
    finally:
        await hub.stop()
    assert hub.closing


class Flaky:
    """An engine whose first connections fail, as when the database is down for a while."""

    def __init__(self, engine: Any, failures: int) -> None:
        self.engine, self.failures = engine, failures

    def connect(self) -> Any:
        if self.failures > 0:
            self.failures -= 1
            raise OSError("database down")
        return self.engine.connect()


async def test_a_good_connection_resets_the_reconnect_backoff(
    jenv: JobsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "RECONNECT_MIN_SECONDS", 0.2)
    hub = EventHub(Flaky(jenv.engine, 3))  # pyright: ignore[reportArgumentType]
    hub.start()
    try:
        await until(lambda: hub.listening, 10)  # after waits of 0.2 + 0.4 + 0.8 s
        kill_listeners(jenv.admin)
        await until(lambda: not hub.listening)
        started = asyncio.get_running_loop().time()
        await until(lambda: hub.listening, 10)
        # back after the minimum wait, not after the 1.6 s the failed attempts had built up
        assert asyncio.get_running_loop().time() - started < 1.0
    finally:
        await hub.stop()


async def test_close_threadsafe_before_start_is_a_no_op(jenv: JobsEnv) -> None:
    hub = EventHub(jenv.engine)
    hub.close_threadsafe()
    assert not hub.closing
    hub.close()
    assert hub.closing
