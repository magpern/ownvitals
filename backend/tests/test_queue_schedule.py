"""The scheduler's definer function, ``queue_schedule`` (#164; PLAN section 4.4, ADR 0002
section 4): one poll or reconciliation per connected connection that is due, counts only.

Synthetic owners and connections; the credential columns hold labelled filler bytes, not
ciphertext. No network.
"""

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from jobtools import (  # noqa: F401 - fixtures
    admin_fixture,
    engine_for,
    ensure_user,
    new_job,
    queue_schema_fixture,
)
from ownvitals.config import Settings
from ownvitals.db import create_engine, create_session_factory
from ownvitals.jobs import queue
from ownvitals.worker.kinds import SCHEDULES

SCHEDULE = "SELECT public.queue_schedule(:kind, :interval)"
SIX_HOURS = 6 * 3600


@pytest.fixture
def app(app_database_url: str) -> Iterator[Engine]:
    with engine_for(app_database_url) as engine:
        yield engine


@pytest.fixture(autouse=True)
def _no_connections(admin: Connection) -> None:
    admin.execute(text("TRUNCATE connections CASCADE"))


def schedule(app: Engine, kind: str, interval: int = SIX_HOURS) -> Any:
    with app.begin() as connection:
        return connection.execute(text(SCHEDULE), {"kind": kind, "interval": interval}).scalar()


def connection_row(admin: Connection, *, status: str = "connected") -> tuple[uuid.UUID, uuid.UUID]:
    """A synthetic connection. A connected row needs credential columns: filler bytes."""
    owner, row_id = uuid.uuid4(), uuid.uuid4()
    ensure_user(admin, owner)
    filler = status != "disconnected"
    admin.execute(
        text(
            "INSERT INTO connections (id, owner_id, provider, status, secret, secret_nonce, "
            "wrapped_dek, dek_nonce, key_id) VALUES (:id, :owner, 'withings', :status, "
            ":secret, :nonce, :wrapped, :nonce, :key)"
        ),
        {
            "id": row_id,
            "owner": owner,
            "status": status,
            "secret": b"\x01" * 16 if filler else None,
            "nonce": b"\x02" * 12 if filler else None,
            "wrapped": b"\x03" * 48 if filler else None,
            "key": "kek-synthetic" if filler else None,
        },
    )
    return owner, row_id


def jobs_for(admin: Connection, connection: uuid.UUID) -> list[Any]:
    return list(
        admin.execute(
            text(
                "SELECT owner_id, kind, origin, lane, state, input, singleton_key, "
                "target_secret_id, max_attempts FROM jobs "
                "WHERE input ->> 'connection_id' = :c ORDER BY created_at"
            ),
            {"c": str(connection)},
        ).all()
    )


def test_each_connected_connection_gets_one_due_job_and_the_count_only(
    admin: Connection, app: Engine
) -> None:
    owner, live = connection_row(admin)
    _, waiting = connection_row(admin, status="needs_reconnect")
    _, gone = connection_row(admin, status="disconnected")
    assert schedule(app, "withings_poll") == 1
    [poll] = jobs_for(admin, live)
    assert (poll.owner_id, poll.kind, poll.origin, poll.lane, poll.state) == (
        owner,
        "withings_poll",
        "scheduler",
        3,
        "queued",
    )
    assert poll.input == {"connection_id": str(live)}
    assert (poll.singleton_key, poll.target_secret_id, poll.max_attempts) == (None, None, 3)
    assert jobs_for(admin, waiting) == []
    assert jobs_for(admin, gone) == []
    # The reconciliation is its own kind and lane.
    assert schedule(app, "withings_reconcile", 24 * 3600) == 1
    kinds = [(row.kind, row.lane) for row in jobs_for(admin, live)]
    assert kinds == [("withings_poll", 3), ("withings_reconcile", 4)]


def test_a_second_call_within_the_interval_adds_nothing(admin: Connection, app: Engine) -> None:
    _, live = connection_row(admin)
    assert schedule(app, "withings_poll") == 1
    assert schedule(app, "withings_poll") == 0  # still queued
    admin.execute(
        text("UPDATE jobs SET state = 'completed', finished_at = now() WHERE kind = :k"),
        {"k": "withings_poll"},
    )
    assert schedule(app, "withings_poll") == 0  # finished, but created within the interval
    admin.execute(
        text("UPDATE jobs SET created_at = now() - interval '7 hours' WHERE kind = :k"),
        {"k": "withings_poll"},
    )
    assert schedule(app, "withings_poll") == 1  # due again
    assert len(jobs_for(admin, live)) == 2


def test_a_running_job_is_not_doubled_however_old(admin: Connection, app: Engine) -> None:
    _, live = connection_row(admin)
    assert schedule(app, "withings_reconcile", 24 * 3600) == 1
    admin.execute(
        text(
            "UPDATE jobs SET state = 'running', attempt = 1, leased_by = 'synthetic-worker', "
            "lease_expires_at = now() + interval '1 minute', heartbeat_at = now(), "
            "lease_version = 1, created_at = now() - interval '3 days'"
        )
    )
    assert schedule(app, "withings_reconcile", 24 * 3600) == 0
    assert len(jobs_for(admin, live)) == 1


def import_job(
    admin: Connection, owner: uuid.UUID, connection: uuid.UUID, kind: str, state: str
) -> uuid.UUID:
    """A synthetic import job of ``connection``: the connect (as the OAuth callback queues it) or
    one stream's backfill (as the connect queues it), in ``state``."""
    payload: dict[str, str] = {"connection_id": str(connection)}
    if kind == "withings_backfill":
        payload["stream"] = "measures"
    columns: dict[str, Any] = {"kind": kind, "state": state, "input": json.dumps(payload)}
    now = datetime.now(UTC)
    if state == "running":
        columns |= {
            "attempt": 1,
            "leased_by": "synthetic-worker",
            "lease_expires_at": now + timedelta(minutes=1),
            "heartbeat_at": now,
            "lease_version": 1,
        }
    elif state != "queued":
        columns |= {"attempt": 1, "finished_at": now}
        if state in {"failed", "dead_letter"}:
            columns["error_category"] = "rate_limited"
    return new_job(admin, owner, **columns)


@pytest.mark.parametrize("kind", ["withings_connect", "withings_backfill"])
@pytest.mark.parametrize("state", ["queued", "running"])
def test_an_active_import_keeps_the_poll_and_the_reconcile_away(
    admin: Connection, app: Engine, kind: str, state: str
) -> None:
    """#407: the first tick after a connect found the new connection due. Its poll queued a
    second backfill per unfinished stream, and its reconcile outranked the backfills in the rate
    limiter. While the import is active the connection gets neither; it stays due."""
    owner, live = connection_row(admin)
    started = import_job(admin, owner, live, kind, state)
    assert schedule(app, "withings_poll") == 0
    assert schedule(app, "withings_reconcile", 24 * 3600) == 0
    assert [row.kind for row in jobs_for(admin, live)] == [kind]
    admin.execute(
        text(
            "UPDATE jobs SET state = 'completed', finished_at = now(), leased_by = NULL, "
            "lease_expires_at = NULL, heartbeat_at = NULL WHERE id = :id"
        ),
        {"id": started},
    )
    assert schedule(app, "withings_poll") == 1
    assert schedule(app, "withings_reconcile", 24 * 3600) == 1


@pytest.mark.parametrize("state", ["completed", "failed", "dead_letter", "cancelled"])
def test_a_finished_import_does_not_hold_the_schedule(
    admin: Connection, app: Engine, state: str
) -> None:
    owner, live = connection_row(admin)
    import_job(admin, owner, live, "withings_backfill", state)
    assert schedule(app, "withings_poll") == 1


def test_an_import_holds_only_its_own_connection(admin: Connection, app: Engine) -> None:
    """One connection's import never holds another connection's schedule."""
    owner, importing = connection_row(admin)
    _, other = connection_row(admin)
    import_job(admin, owner, importing, "withings_backfill", "queued")
    assert schedule(app, "withings_poll") == 1
    assert [row.kind for row in jobs_for(admin, importing)] == ["withings_backfill"]
    assert [row.kind for row in jobs_for(admin, other)] == ["withings_poll"]


def test_another_owners_import_naming_the_same_connection_does_not_hold_it(
    admin: Connection, app: Engine
) -> None:
    """The hold is per owner: a job of another owner that names this connection id holds nothing."""
    _, live = connection_row(admin)
    stranger = uuid.uuid4()
    ensure_user(admin, stranger)
    import_job(admin, stranger, live, "withings_backfill", "queued")
    assert schedule(app, "withings_poll") == 1
    assert [row.kind for row in jobs_for(admin, live) if row.owner_id != stranger] == [
        "withings_poll"
    ]


def test_an_import_naming_another_connection_does_not_hold_this_one(
    admin: Connection, app: Engine
) -> None:
    """The hold is per connection: the owner's import of some other connection holds nothing."""
    owner, live = connection_row(admin)
    import_job(admin, owner, uuid.uuid4(), "withings_backfill", "queued")
    assert schedule(app, "withings_poll") == 1
    assert [row.kind for row in jobs_for(admin, live)] == ["withings_poll"]


def test_another_owners_job_does_not_make_a_connection_look_scheduled(
    admin: Connection, app: Engine
) -> None:
    """The check is per owner and connection: one connection's poll never covers another."""
    _, first = connection_row(admin)
    _, second = connection_row(admin)
    assert schedule(app, "withings_poll") == 2
    assert len(jobs_for(admin, first)) == 1
    assert len(jobs_for(admin, second)) == 1


@pytest.mark.parametrize(
    ("kind", "interval"),
    [
        ("withings_sync", SIX_HOURS),  # only the poll and the reconciliation are scheduled
        ("withings_backfill", SIX_HOURS),
        ("test_kind", SIX_HOURS),
        (None, SIX_HOURS),
        ("withings_poll", 3599),
        ("withings_poll", 604_801),
        ("withings_poll", None),
    ],
)
def test_other_kinds_and_intervals_are_refused(
    admin: Connection, app: Engine, kind: str | None, interval: int | None
) -> None:
    connection_row(admin)
    with pytest.raises(DBAPIError, match="invalid"):
        schedule(app, kind, interval)  # type: ignore[arg-type]
    assert admin.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0


def test_only_the_app_role_and_the_queue_role_may_run_it(admin: Connection) -> None:
    acl = admin.execute(
        text(
            "SELECT p.proacl::text, p.proowner::regrole::text, p.prosecdef "
            "FROM pg_proc p WHERE p.oid = 'public.queue_schedule(text,integer)'::regprocedure"
        )
    ).one()
    assert acl.proowner == "ownvitals_queue"
    assert acl.prosecdef is True
    assert acl.proacl == "{ownvitals_queue=X/ownvitals_queue,ownvitals_app=X/ownvitals_queue}"


async def test_the_python_call_returns_the_count(admin: Connection, app_database_url: str) -> None:
    connection_row(admin)
    engine = create_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        factory = create_session_factory(engine)
        counts = await asyncio.gather(
            *(queue.schedule(factory, kind, seconds) for kind, seconds in SCHEDULES.items())
        )
    finally:
        await engine.dispose()
    assert counts == [1, 1]
    rows = admin.execute(text("SELECT kind, input FROM jobs ORDER BY lane")).all()
    assert [row.kind for row in rows] == ["withings_poll", "withings_reconcile"]
    assert all(json.dumps(row.input).count("connection_id") == 1 for row in rows)
