"""The job tables of ADR 0005 section 1: guard 1, checks, append-only events, cascades, and
isolation between two users in the worker's own context.

Acceptance criterion 4 of #9 is ``test_a_job_run_sees_and_writes_only_its_owners_rows``.
Synthetic rows only: owners are random UUIDs (no ``users`` table before #10).
"""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from jobcore import (
    JobsEnv,
    claim,
    jenv_fixture,  # noqa: F401 - fixtures
    registry_for,
)
from jobtools import admin_fixture, as_user, engine_for, new_job, new_user, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.context import UserContext, issuer_token
from ownvitals.db import SYSTEM_TABLES, system_session, user_session
from ownvitals.jobs import Complete, JobRun
from ownvitals.jobs.runner import execute
from rls_guard import check_tables

JOB_TABLES = ("jobs", "job_events")


@pytest.fixture
def app(app_database_url: str) -> Iterator[Engine]:
    with engine_for(app_database_url) as engine:
        yield engine


def add_event(
    conn: Connection, owner: uuid.UUID, job_id: uuid.UUID, seq: int = 1, **columns: Any
) -> None:
    values: dict[str, Any] = {
        "owner_id": owner,
        "job_id": job_id,
        "seq": seq,
        "kind": "queued",
        "lease_version": 0,
        **columns,
    }
    names = ", ".join(values)
    params = ", ".join(f"CAST(:{k} AS jsonb)" if k == "payload" else f":{k}" for k in values)
    conn.execute(text(f"INSERT INTO job_events ({names}) VALUES ({params})"), values)  # noqa: S608


def test_the_job_tables_meet_guard_1_with_exactly_their_deny_policies(admin: Connection) -> None:
    assert check_tables(admin, SYSTEM_TABLES) == []
    for table in JOB_TABLES:
        assert table not in SYSTEM_TABLES
    policies = admin.execute(
        text(
            "SELECT tablename, policyname, permissive, cmd, roles::text[], qual FROM pg_policies "
            "WHERE tablename = ANY(:t) AND policyname <> 'owner_isolation'"
        ),
        {"t": list(JOB_TABLES)},
    ).all()
    assert sorted(tuple(p) for p in policies) == [
        ("job_events", "events_no_delete", "RESTRICTIVE", "DELETE", ["public"], "false"),
        ("job_events", "events_no_update", "RESTRICTIVE", "UPDATE", ["public"], "false"),
    ]


@pytest.mark.parametrize(
    ("policy", "cmd"), [("events_no_update", "UPDATE"), ("events_no_delete", "DELETE")]
)
def test_guard_1_requires_the_deny_policies(admin: Connection, policy: str, cmd: str) -> None:
    admin.execute(text(f"DROP POLICY {policy} ON public.job_events"))
    try:
        found = check_tables(admin, SYSTEM_TABLES)
        assert found == [
            f"job_events: needs a restrictive FOR {cmd} TO PUBLIC USING (false) policy"
        ]
    finally:
        admin.execute(
            text(
                f"CREATE POLICY {policy} ON public.job_events AS RESTRICTIVE FOR {cmd} TO PUBLIC "
                "USING (false)"
            )
        )
    assert check_tables(admin, SYSTEM_TABLES) == []


def test_the_owner_id_has_no_foreign_key_until_the_accounts_issue(admin: Connection) -> None:
    """Pins the seam (#77): #10 adds the ``users`` table and ``jobs.owner_id``'s foreign key with
    ``ON DELETE CASCADE``; until then the only foreign key is the events' owner-inclusive one."""
    keys = admin.execute(
        text(
            "SELECT conrelid::regclass::text, conname, confrelid::regclass::text, confdeltype "
            "FROM pg_constraint WHERE contype = 'f' AND conrelid = ANY(CAST(:t AS regclass[]))"
        ),
        {"t": list(JOB_TABLES)},
    ).all()
    assert [tuple(k) for k in keys] == [("job_events", "job_events_job_fkey", "jobs", "c")]


def test_job_events_are_append_only_for_every_role_but_go_with_their_job(
    admin: Connection, app: Engine, owner_database_url: str
) -> None:
    owner = new_user()
    first, second = new_job(admin, owner), new_job(admin, owner)
    with app.begin() as connection:  # the app role, as the owner, under RLS
        as_user(connection, owner)
        add_event(connection, owner, first)
        add_event(connection, owner, second)
        assert connection.execute(text("UPDATE job_events SET seq = 9")).rowcount == 0
        assert connection.execute(text("DELETE FROM job_events")).rowcount == 0
        assert connection.execute(text("SELECT count(*) FROM job_events")).scalar_one() == 2
    with engine_for(owner_database_url) as engine, engine.begin() as connection:
        as_user(connection, owner)  # FORCE: the table owner is held to the policies too
        assert connection.execute(text("UPDATE job_events SET seq = 9")).rowcount == 0
        assert connection.execute(text("DELETE FROM job_events")).rowcount == 0
    with app.begin() as connection:
        as_user(connection, owner)
        connection.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": first})
        assert connection.execute(text("SELECT job_id FROM job_events")).scalars().all() == [second]
        seqs = connection.execute(text("SELECT seq FROM job_events")).scalars().all()
        assert seqs == [1]


def test_an_event_belongs_to_a_job_of_its_own_owner(admin: Connection) -> None:
    owner, other = new_user(), new_user()
    job_id = new_job(admin, owner)
    with pytest.raises(IntegrityError, match="job_events_job_fkey"):
        add_event(admin, other, job_id)  # the owner-inclusive key
    add_event(admin, owner, job_id)
    with pytest.raises(IntegrityError, match="job_events_owner_id_job_id_seq_key"):
        add_event(admin, owner, job_id)  # one event per seq


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"attempt": 4, "max_attempts": 3}, "jobs_attempt"),
        ({"attempt": -1}, "jobs_attempt"),
        ({"max_attempts": 11}, "jobs_max_attempts"),
        ({"max_attempts": 0, "attempt": 0}, "jobs_max_attempts"),
        ({"kind": "Bad-Kind"}, "jobs_kind"),
        ({"kind": "k" * 41}, "jobs_kind"),
        ({"state": "paused"}, "jobs_state"),
        ({"idempotency_key": "short"}, "jobs_idempotency_key"),
        ({"idempotency_key": "has a space in it ok"}, "jobs_idempotency_key"),
        ({"request_hash": b"x"}, "jobs_request_hash"),
        ({"lease_version": -1}, "jobs_lease_version"),
        ({"next_seq": -1}, "jobs_next_seq"),
        ({"state": "running"}, "jobs_lease"),
        ({"leased_by": "w1"}, "jobs_lease"),
        ({"heartbeat_at": "2026-10-31"}, "jobs_lease"),
        ({"state": "failed", "finished_at": "2026-10-28"}, "jobs_failed"),
        ({"error_category": "internal"}, "jobs_failed"),
        (
            {"state": "failed", "finished_at": "2026-10-28", "error_category": "made_up"},
            "jobs_error_category",
        ),
        ({"state": "completed"}, "jobs_finished"),
        ({"finished_at": "2026-10-28"}, "jobs_finished"),
        ({"input": '"' + "x" * 16384 + '"'}, "jobs_input"),
    ],
)
def test_the_job_checks_refuse(admin: Connection, columns: dict[str, Any], constraint: str) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        new_job(admin, new_user(), **columns)


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        ({"seq": 0}, "job_events_seq"),
        ({"kind": "stage_started"}, "job_events_kind"),
        ({"payload": '{"x": "' + "y" * 2048 + '"}'}, "job_events_payload"),
        ({"lease_version": -1}, "job_events_lease_version"),
    ],
)
def test_the_event_checks_refuse(
    admin: Connection, columns: dict[str, Any], constraint: str
) -> None:
    owner = new_user()
    job_id = new_job(admin, owner)
    with pytest.raises(IntegrityError, match=constraint):
        add_event(admin, owner, job_id, **columns)


def test_the_idempotency_key_is_unique_per_owner_only(admin: Connection) -> None:
    owner, other = new_user(), new_user()
    key = "k" * 20
    new_job(admin, owner, idempotency_key=key)
    new_job(admin, other, idempotency_key=key)
    with pytest.raises(IntegrityError, match="jobs_owner_id_idempotency_key_key"):
        new_job(admin, owner, idempotency_key=key)


def ctx_of(owner: uuid.UUID) -> UserContext:
    return UserContext(user_id=owner, role="user", issuer=issuer_token())


async def test_a_job_run_sees_and_writes_only_its_owners_rows(jenv: JobsEnv) -> None:
    """Acceptance 4, in the worker's own context: the worker engine (the app role with the role
    and queue checks), the issuer's context and the fenced transactions of a real run. A's run
    sees only A's jobs and events, cannot change B's job, and cannot write an event as B or for
    B's job; B's rows are untouched."""
    a, b = new_user(), new_user()
    _, job_a = jenv.job(a)
    _, job_b = jenv.job(b)
    jenv.sql(
        "INSERT INTO job_events (owner_id, job_id, seq, kind, lease_version) "
        "VALUES (:o, :j, 1, 'queued', 0)",
        o=b,
        j=job_b,
    )
    jenv.sql("UPDATE jobs SET next_seq = 1 WHERE id = :id", id=job_b)
    jenv.sql(
        "UPDATE jobs SET available_at = now() + interval '1 hour' WHERE id = :id", id=job_b
    )  # so the claim below takes A's job
    before_b = (jenv.row(job_b), jenv.events(job_b))
    seen: dict[str, Any] = {}

    async def run(job_run: JobRun) -> Complete:
        assert job_run.ctx.user_id == a
        async with job_run.fenced() as fs:
            db = fs.db
            seen["jobs"] = set((await db.execute(text("SELECT id FROM jobs"))).scalars())
            seen["events"] = set(
                (await db.execute(text("SELECT DISTINCT job_id FROM job_events"))).scalars()
            )
            seen["owners"] = set(
                (await db.execute(text("SELECT DISTINCT owner_id FROM job_events"))).scalars()
            )
            changed = await db.execute(
                text("UPDATE jobs SET cancel_requested_at = now() WHERE id = :id"), {"id": job_b}
            )
            seen["updated"] = changed.rowcount  # pyright: ignore[reportAttributeAccessIssue]
        for owner, job_id in ((b, job_b), (b, job_a), (a, job_b)):
            with pytest.raises(DBAPIError):
                async with job_run.fenced() as fs:
                    await fs.db.execute(
                        text(
                            "INSERT INTO job_events (owner_id, job_id, seq, kind, lease_version) "
                            "VALUES (:o, :j, 99, 'progress', 1)"
                        ),
                        {"o": owner, "j": job_id},
                    )
        await job_run.progress(1, 1)
        return Complete()

    execution = await claim(jenv)
    assert execution.claimed.job_id == job_a
    await execute(execution, jenv.factory, registry_for(run), jenv.config(), asyncio.Event())
    assert seen == {"jobs": {job_a}, "events": {job_a}, "owners": {a}, "updated": 0}
    assert execution.result == "completed"
    assert jenv.kinds(job_a) == ["claimed", "progress", "completed"]
    assert (jenv.row(job_b), jenv.events(job_b)) == before_b


async def test_the_sessions_see_only_their_owners_jobs(jenv: JobsEnv) -> None:
    """The same with plain sessions on the worker engine: a user session sees its owner's rows,
    the system session (no ``app.user_id``) none at all."""
    a, b = new_user(), new_user()
    _, job_a = jenv.job(a)
    _, job_b = jenv.job(b)
    async with user_session(jenv.factory, ctx_of(a)) as db:
        assert set((await db.execute(text("SELECT id FROM jobs"))).scalars()) == {job_a}
    async with user_session(jenv.factory, ctx_of(b)) as db:
        assert set((await db.execute(text("SELECT id FROM jobs"))).scalars()) == {job_b}
    with pytest.raises(ProgrammingError, match="row-level security"):
        async with user_session(jenv.factory, ctx_of(b)) as db:
            await db.execute(
                text(
                    "INSERT INTO jobs (owner_id, kind, state, input, idempotency_key, "
                    "request_hash) VALUES (:o, 'test_kind', 'queued', '{}', :k, :h)"
                ),
                {"o": a, "k": "x" * 20, "h": b"\x00" * 32},
            )
    async with system_session(jenv.factory) as db:
        assert (await db.execute(text("SELECT count(*) FROM jobs"))).scalar_one() == 0
        assert (await db.execute(text("SELECT count(*) FROM job_events"))).scalar_one() == 0
