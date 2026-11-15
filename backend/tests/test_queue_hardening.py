"""Queue hardening (#278, ADR 0005 sections 3, 8 and 9): the cross-owner cancel waits for a
locked queued row, replay takes the job-creation lock, an enqueue that meets a row requeued after
its check collapses into it, a job that targets a webhook secret must name it, ``jobs replay``
audits before it commits, and a dead-lettered job is not cancellable.

Synthetic owners, connections and secret ids only; the kind name ``notify_update`` is a label.
No network and no secret values.
"""

import io
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError

from admintools import drop_maint, install_maint
from jobapi import (  # noqa: F401 - fixtures
    Api,
    api_fixture,
    bare_app_fixture,
)
from jobcore import JobsEnv, jenv_fixture, settings_for  # noqa: F401 - fixtures
from jobtools import (  # noqa: F401 - fixtures
    admin_fixture,
    admin_url,
    engine_for,
    ensure_user,
    events,
    job,
    new_job,
    new_user,
    queue_schema_fixture,
)
from ownvitals import cli
from ownvitals.admin.db import MaintRoleError
from ownvitals.jobs.withings import singleton_for

SECRET = uuid.UUID("00000000-0000-4000-8000-000000000278")
ENQUEUE = (
    "SELECT public.queue_enqueue(:connection, :kind, 'scheduler', CAST(:payload AS jsonb), "
    ":singleton, 3, :secret)"
)
WAIT_SECONDS = 10


@pytest.fixture
def app(app_database_url: str) -> Iterator[Engine]:
    with engine_for(app_database_url) as engine:
        yield engine


def call(app: Engine, sql: str, **params: Any) -> list[Any]:
    with app.begin() as connection:
        return list(connection.execute(text(sql), params).all())


def connect_row(admin: Connection, owner: uuid.UUID) -> uuid.UUID:
    ensure_user(admin, owner)
    row_id = uuid.uuid4()
    admin.execute(
        text(
            "INSERT INTO connections (id, owner_id, provider, status) "
            "VALUES (:id, :owner, 'test', 'needs_reconnect')"
        ),
        {"id": row_id, "owner": owner},
    )
    return row_id


class Background:
    """One blocking call in a thread; its result or its exception."""

    def __init__(self, work: Callable[[], Any]) -> None:
        self.result: Any = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(work,))
        self._thread.start()

    def _run(self, work: Callable[[], Any]) -> None:
        try:
            self.result = work()
        except BaseException as exc:  # handed to the test thread's caller
            self.error = exc

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()

    def join(self) -> Any:
        self._thread.join(WAIT_SECONDS)
        assert self.done, "the call did not finish"
        if self.error is not None:
            raise self.error
        return self.result


def waited_on(admin: Connection, call: Background, wait_event: str) -> bool:
    """Whether an app-role backend waited on a lock of ``wait_event`` before ``call`` finished:
    polled in ``pg_stat_activity``. A call that never waits finishes first: ``False``."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        waiting = admin.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = 'ownvitals_app' "
                "AND wait_event_type = 'Lock' AND wait_event = :event"
            ),
            {"event": wait_event},
        ).scalar_one()
        if waiting:
            return True
        if call.done:
            return False
        time.sleep(0.02)
    return False


@pytest.fixture
def locker(queue_schema: str) -> Iterator[Connection]:
    """A second superuser connection whose transaction the test commits itself."""
    with engine_for(queue_schema) as engine, engine.connect() as connection:
        yield connection
        connection.rollback()


def notify_job(admin: Connection, owner: uuid.UUID) -> uuid.UUID:
    return new_job(
        admin,
        owner,
        kind="notify_update",
        origin="webhook",
        idempotency_key=None,
        request_hash=None,
        target_secret_id=SECRET,
    )


@pytest.mark.parametrize("claimed_meanwhile", [False, True])
def test_cancel_for_a_secret_waits_for_a_locked_queued_row(
    admin: Connection, app: Engine, locker: Connection, claimed_meanwhile: bool
) -> None:
    """Was ``SKIP LOCKED``: a queued row held by a debounce or a claim stayed queued."""
    free, held = notify_job(admin, new_user()), notify_job(admin, new_user())
    locker.execute(text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": held})
    cancel = Background(
        lambda: call(app, "SELECT public.queue_cancel_for_secret('notify_update', :s)", s=SECRET)
    )
    assert waited_on(admin, cancel, "transactionid")
    if claimed_meanwhile:
        locker.execute(
            text(
                "UPDATE jobs SET state = 'running', attempt = 1, leased_by = 'w', "
                "lease_expires_at = now() + interval '1 minute', heartbeat_at = now() "
                "WHERE id = :id"
            ),
            {"id": held},
        )
    locker.commit()
    [(count,)] = cancel.join()
    assert job(admin, free).state == "cancelled"
    if claimed_meanwhile:  # no longer queued when its lock went: left to the runner's re-check
        assert (count, job(admin, held).state) == (1, "running")
    else:
        assert (count, job(admin, held).state) == (2, "cancelled")
        assert events(admin, held)[-1][1] == "cancelled"


def test_replay_takes_the_job_creation_lock(
    admin: Connection, app: Engine, locker: Connection
) -> None:
    """ADR 0005 section 9, namespace 1 keyed by the owner, as ``queue_enqueue`` takes it."""
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
        singleton_key="synthetic:measures",
    )
    locker.execute(text("SELECT pg_advisory_xact_lock(1, hashtext(:owner))"), {"owner": str(owner)})
    replay = Background(lambda: call(app, "SELECT job_id FROM public.queue_replay(:id)", id=dead))
    assert waited_on(admin, replay, "advisory")
    locker.commit()
    [(created,)] = replay.join()
    assert job(admin, created).state == "queued"
    assert job(admin, dead).state == "dead_letter"


def test_an_enqueue_collapses_into_a_row_requeued_after_its_check(
    admin: Connection, app: Engine, locker: Connection
) -> None:
    """A running singleton handed back between the enqueue's check and its insert: the insert
    meets ``jobs_singleton_queued``. It collapses into that row instead of failing."""
    owner = new_user()
    row = connect_row(admin, owner)
    key = singleton_for(row, "measures")
    running = new_job(
        admin,
        owner,
        kind="withings_sync",
        origin="webhook",
        idempotency_key=None,
        request_hash=None,
        singleton_key=key,
        state="running",
        attempt=1,
        lease_version=1,
        leased_by="w",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        heartbeat_at=datetime.now(UTC),
    )
    locker.execute(
        text(
            "UPDATE jobs SET state = 'queued', attempt = 0, lease_version = 2, leased_by = NULL, "
            "lease_expires_at = NULL, heartbeat_at = NULL WHERE id = :id"
        ),
        {"id": running},
    )
    payload = f'{{"connection_id":"{row}","stream":"measures"}}'
    enqueue = Background(
        lambda: call(
            app,
            ENQUEUE,
            connection=row,
            kind="withings_sync",
            payload=payload,
            singleton=key,
            secret=None,
        )
    )
    assert waited_on(admin, enqueue, "transactionid")
    locker.commit()
    [(collapsed,)] = enqueue.join()
    assert collapsed == running
    assert admin.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
    seconds = admin.execute(
        text("SELECT extract(epoch FROM available_at - now()) FROM jobs WHERE id = :id"),
        {"id": running},
    ).scalar_one()
    assert 29 < float(seconds) <= 31


def test_a_job_that_targets_a_secret_must_name_it(admin: Connection, app: Engine) -> None:
    """A null or different ``p_target_secret_id`` escaped the cancel and the runner re-check."""
    row = connect_row(admin, new_user())
    named = f'{{"connection_id":"{row}","secret_id":"{SECRET}"}}'
    plain = f'{{"connection_id":"{row}"}}'
    for kind, payload, secret in (
        ("notify_update", named, None),
        ("notify_update", named, uuid.uuid4()),
        ("notify_update", plain, SECRET),
        ("notify_update", plain, None),
        ("withings_poll", named, None),
        ("withings_poll", plain, SECRET),
    ):
        with pytest.raises(DBAPIError, match="invalid target"):
            call(
                app,
                ENQUEUE,
                connection=row,
                kind=kind,
                payload=payload,
                singleton=None,
                secret=secret,
            )
    assert admin.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0
    [(stored,)] = call(
        app,
        ENQUEUE,
        connection=row,
        kind="notify_update",
        payload=named,
        singleton=None,
        secret=SECRET,
    )
    assert job(admin, stored).target_secret_id == SECRET
    [(cancelled,)] = call(
        app, "SELECT public.queue_cancel_for_secret('notify_update', :s)", s=SECRET
    )
    assert (cancelled, job(admin, stored).state) == (1, "cancelled")


@pytest.fixture
def maint_settings(admin: Connection, app_database_url: str) -> Iterator[Any]:
    del admin  # empty job tables
    maint_url = install_maint(admin_url())
    try:
        yield settings_for(
            app_database_url,
            maint_database_url=maint_url,
            database_maint_password=make_url(maint_url).password,
        )
    finally:
        drop_maint(admin_url())


def dead_letter(admin: Connection) -> tuple[uuid.UUID, uuid.UUID]:
    owner = new_user()
    return owner, new_job(
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


def audit_rows(admin: Connection, dead: uuid.UUID) -> list[Any]:
    return list(
        admin.execute(
            text(
                "SELECT action, outcome, target_user_id, params FROM admin_audit "
                "WHERE target_kind = 'job' AND target_id = :id"
            ),
            {"id": str(dead)},
        ).all()
    )


async def replay(settings: Any, dead: uuid.UUID) -> tuple[int, str, str]:
    import argparse

    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(command="jobs", action="replay", job_id=str(dead))
    code = await cli.run_replay(args, settings, out, err)
    return code, out.getvalue(), err.getvalue()


async def test_jobs_replay_writes_its_audit_row(admin: Connection, maint_settings: Any) -> None:
    owner, dead = dead_letter(admin)
    code, out, err = await replay(maint_settings, dead)
    assert (code, err) == (0, "")
    created = uuid.UUID(out.strip())
    assert (job(admin, created).state, job(admin, created).origin) == ("queued", "operator")
    [row] = audit_rows(admin, dead)
    assert (row.action, row.outcome, row.target_user_id) == ("job.replay", "ok", owner)
    assert row.params["new_job_id"] == str(created)


async def test_a_replay_whose_audit_fails_queues_nothing(
    admin: Connection, maint_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Was: the new job committed first, so a failed audit left an unaudited replay queued."""
    _, dead = dead_letter(admin)

    async def refused(*_: object) -> None:
        raise MaintRoleError("synthetic: the admin role is refused")

    monkeypatch.setattr(cli, "record_operator_action", refused)
    code, out, err = await replay(maint_settings, dead)
    assert (code, out) == (1, "")
    assert "the audit row was not written" in err
    assert admin.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
    assert job(admin, dead).state == "dead_letter"
    assert audit_rows(admin, dead) == []


async def test_a_dead_lettered_job_is_not_cancellable(jenv: JobsEnv, api: Api) -> None:
    """Was a 200 that changed nothing; the other ended states already answer 409."""
    alice = api.browser()
    _, dead = jenv.job(
        alice.user_id,
        state="dead_letter",
        error_category="upstream_unavailable",
        finished_at="2026-11-02",
        attempt=3,
        max_attempts=3,
    )
    response = await alice.post(f"/api/jobs/{dead}/cancel")
    assert (response.status_code, response.json()) == (
        409,
        {"detail": {"reason": "not_cancellable"}},
    )
    assert jenv.row(dead).state == "dead_letter"
    assert jenv.kinds(dead) == []
