"""Withings queue extensions (ADR 0005 section 8, #18): singletons, lanes, dead-letter, replay
and the cross-owner cancel of jobs that target a webhook secret.

Synthetic owners and the test-only kind, plus the kind name ``notify_update`` as a label only.
No network and no secret values.
"""

import asyncio
import threading
import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError

from admintools import drop_maint, install_maint
from jobcore import (  # noqa: F401 - fixtures
    JobsEnv,
    jenv_fixture,
    registry_for,
    run_next,
)
from jobtools import (  # noqa: F401 - fixtures
    QUEUE_SCRIPT,
    admin_fixture,
    admin_url,
    as_user,
    engine_for,
    ensure_user,
    events,
    job,
    new_job,
    new_user,
    queue_schema_fixture,
)
from ownvitals.admin.audit import Target
from ownvitals.admin.db import AdminDb, record_operator_action
from ownvitals.jobs import Complete, JobRun
from ownvitals.jobs.runner import RunConfig
from ownvitals.jobs.withings import lane_for, singleton_for

ENQUEUE = (
    "SELECT public.queue_enqueue(:connection, :kind, :origin, CAST(:payload AS jsonb), "
    ":singleton, :attempts, :secret)"
)
SECRET = uuid.UUID("00000000-0000-4000-8000-000000000018")


@pytest.fixture
def app(app_database_url: str) -> Iterator[Engine]:
    with engine_for(app_database_url) as engine:
        yield engine


def call(app: Engine, sql: str, **params: Any) -> list[Any]:
    with app.begin() as connection:
        return list(connection.execute(text(sql), params).all())


def connect_row(
    admin: Connection, owner: uuid.UUID, *, status: str = "needs_reconnect"
) -> uuid.UUID:
    ensure_user(admin, owner)
    row_id = uuid.uuid4()
    admin.execute(
        text(
            "INSERT INTO connections (id, owner_id, provider, status) "
            "VALUES (:id, :owner, 'test', :status)"
        ),
        {"id": row_id, "owner": owner, "status": status},
    )
    return row_id


def enqueue(
    app: Engine,
    connection_id: uuid.UUID,
    *,
    kind: str = "withings_sync",
    origin: str = "webhook",
    stream: str = "measures",
) -> uuid.UUID:
    payload = f'{{"connection_id":"{connection_id}","stream":"{stream}"}}'
    return call(
        app,
        ENQUEUE,
        connection=connection_id,
        kind=kind,
        origin=origin,
        payload=payload,
        singleton=singleton_for(connection_id, stream) if kind == "withings_sync" else None,
        attempts=3,
        secret=None,
    )[0][0]


def seconds_until(admin: Connection, job_id: uuid.UUID) -> float:
    return float(
        admin.execute(
            text("SELECT extract(epoch FROM available_at - now()) FROM jobs WHERE id = :id"),
            {"id": job_id},
        ).scalar_one()
    )


def test_enqueue_takes_the_job_creation_lock() -> None:
    """ADR 0005 section 9: namespace 1, keyed by the owner. The SQL uses the same number."""
    source = QUEUE_SCRIPT.read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock(1, pg_catalog.hashtext(v_owner::text))" in source


def test_duplicate_singleton_enqueues_collapse_and_debounce(admin: Connection, app: Engine) -> None:
    owner = new_user()
    row = connect_row(admin, owner)
    first = enqueue(app, row)
    assert enqueue(app, row) == first
    assert admin.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
    assert 29 < seconds_until(admin, first) <= 31
    stored = job(admin, first)
    assert stored.owner_id == owner
    assert (stored.origin, stored.lane, stored.state) == (
        "webhook",
        lane_for("withings_sync", "webhook"),
        "queued",
    )
    admin.execute(text("UPDATE jobs SET available_at = now() WHERE id = :id"), {"id": first})
    call(app, "SELECT * FROM public.queue_claim('w1', 60, ARRAY['withings_sync'])")
    follow = enqueue(app, row)
    assert follow != first
    assert job(admin, follow).state == "queued"
    assert job(admin, first).state == "running"
    assert seconds_until(admin, follow) < 2
    assert enqueue(app, row) == follow
    assert admin.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 2
    assert 29 < seconds_until(admin, follow) <= 31


def test_issuer_jobs_are_owned_by_the_connection_under_rls(admin: Connection, app: Engine) -> None:
    owner, other = new_user(), new_user()
    row = connect_row(admin, owner)
    job_id = enqueue(app, row, kind="withings_poll", origin="scheduler")
    assert (job(admin, job_id).owner_id, job(admin, job_id).lane) == (
        owner,
        lane_for("withings_poll", "scheduler"),
    )
    with app.begin() as seen:
        as_user(seen, owner)
        assert seen.execute(text("SELECT id FROM jobs")).scalars().all() == [job_id]
    with app.begin() as hidden:
        as_user(hidden, other)
        assert hidden.execute(text("SELECT id FROM jobs")).scalars().all() == []
    with pytest.raises(DBAPIError, match="unknown connection"):
        enqueue(app, uuid.uuid4(), kind="withings_poll", origin="scheduler")
    disconnected = connect_row(admin, owner, status="disconnected")
    with pytest.raises(DBAPIError, match="unknown connection"):
        enqueue(app, disconnected, kind="withings_poll", origin="scheduler")


def test_lane_order_holds_when_claimers_race(admin: Connection, app: Engine) -> None:
    owner = new_user()
    low = [new_job(admin, owner, lane=5, kind="withings_backfill") for _ in range(8)]
    high = new_job(admin, owner, lane=1, kind="withings_sync")
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def once() -> None:
        rows = call(
            app,
            "SELECT job_id FROM public.queue_claim(:worker, 60, "
            "ARRAY['withings_sync', 'withings_backfill', 'test_kind'])",
            worker=f"w-{uuid.uuid4().hex[:8]}",
        )
        with lock:
            claimed.extend(row.job_id for row in rows)

    def run() -> None:
        barrier.wait()
        once()

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert high in claimed
    assert len(claimed) == 3
    assert all(item in {*low, high} for item in claimed)
    assert job(admin, high).state == "running"


def test_cancel_for_a_secret_counts_across_owners(admin: Connection, app: Engine) -> None:
    owners = (new_user(), new_user())
    queued = [
        new_job(
            admin,
            owner,
            kind="notify_update",
            origin="webhook",
            idempotency_key=None,
            request_hash=None,
            target_secret_id=SECRET,
        )
        for owner in owners
    ]
    other = new_job(admin, new_user(), kind="notify_update", target_secret_id=uuid.uuid4())
    cancelled = call(app, "SELECT public.queue_cancel_for_secret('notify_update', :s)", s=SECRET)[
        0
    ][0]
    assert cancelled == 2
    assert [job(admin, row).state for row in queued] == ["cancelled", "cancelled"]
    assert job(admin, other).state == "queued"
    assert all(events(admin, row)[-1][1] == "cancelled" for row in queued)


async def test_a_run_stops_when_its_target_is_not_active(jenv: JobsEnv) -> None:
    _, retired = jenv.job(target_secret_id=SECRET)
    calls = 0

    async def run(job_run: JobRun) -> Complete:
        nonlocal calls
        calls += 1
        return Complete()

    stopped = await run_next(jenv, registry_for(run))
    assert calls == 0
    assert (stopped.result, jenv.row(retired).state) == ("cancelled", "cancelled")

    _, kept = jenv.job(target_secret_id=uuid.uuid4())

    class Active:
        async def entry_active(self, factory: object, secret_id: UUID) -> bool:
            return secret_id != SECRET

    config = RunConfig(lease_seconds=60, accounts=jenv.accounts, targets=Active())
    finished = await run_next(jenv, registry_for(run), config=config)
    assert calls == 1
    assert finished.result == "completed"
    assert jenv.row(kept).state == "completed"


def test_replay_requeues_a_dead_letter_and_writes_an_audit_row(
    admin: Connection, app: Engine
) -> None:
    owner = new_user()
    dead = new_job(
        admin,
        owner,
        state="dead_letter",
        error_category="upstream_unavailable",
        finished_at="2026-11-02",
        attempt=3,
        max_attempts=3,
        kind="withings_sync",
        lane=2,
    )
    maint_url = install_maint(admin_url())
    try:
        [created] = call(app, "SELECT job_id, owner_id FROM public.queue_replay(:id)", id=dead)
        db = AdminDb(make_url(maint_url))

        async def audit() -> None:
            try:
                await record_operator_action(
                    db,
                    "job.replay",
                    {"dead_letter_id": dead, "new_job_id": created.job_id},
                    Target(user_id=created.owner_id, kind="job", id=str(dead)),
                )
            finally:
                await db.dispose()

        asyncio.run(audit())
    finally:
        drop_maint(admin_url())
    assert job(admin, dead).state == "dead_letter"
    fresh = job(admin, created.job_id)
    assert (fresh.state, fresh.origin, fresh.owner_id, fresh.kind, fresh.lane) == (
        "queued",
        "operator",
        owner,
        "withings_sync",
        2,
    )
    assert fresh.id != dead
    row = admin.execute(
        text("SELECT action, outcome, target_user_id, target_kind FROM admin_audit")
    ).one()
    assert (row.action, row.outcome, row.target_user_id, row.target_kind) == (
        "job.replay",
        "ok",
        owner,
        "job",
    )
    with pytest.raises(DBAPIError, match="not replayable"):
        call(app, "SELECT * FROM public.queue_replay(:id)", id=fresh.id)
