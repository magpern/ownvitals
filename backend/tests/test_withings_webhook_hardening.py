"""The webhook receiver's error paths, the webhook quotas and the append lead (#280).

Synthetic ids and key bytes only (the helpers of ``test_withings_webhook.py``): no real
account, token, webhook secret or measurement.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from io import StringIO
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from psycopg import errors
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError, OperationalError

from jobtools import engine_for
from ownvitals.config import Settings
from ownvitals.db import create_engine as async_engine
from ownvitals.db import create_session_factory
from ownvitals.withings import webhook_receiver
from ownvitals.withings.subscriptions import CurrentWebhookSeed
from ownvitals.withings.webhook_cache import CACHE_TTL
from ownvitals.withings.webhooks import Cache, Factory, append_secret
from test_withings_webhook import (  # pyright: ignore[reportPrivateUsage]
    PEPPER_BYTES,
    SEED_BYTES,
    USERID,
    WHEN,
    captured,  # noqa: F401 - fixture
    counts,
    entry,
    form,
    link,
    post,
    receiver,
)
from withingsschema import PEPPER, SEED, new_app, seed_keys

FORGED = "forged-secret-not-the-derived-one"
PER_CONNECTION = 60
PER_APP = 1000


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = async_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def same_answer(one: httpx.Response, other: httpx.Response) -> None:
    """Status, body and headers: nothing tells the two apart."""
    assert (one.status_code, one.content) == (other.status_code, other.content)
    assert sorted(one.headers.items()) == sorted(other.headers.items())


async def linked_receiver(database_url: str, factory: Factory) -> tuple[FastAPI, str, int]:
    with engine_for(database_url) as engine, engine.begin() as admin:
        app_id = new_app(database_url)
        presented, cached = entry(app_id)
        link(admin, app_id)
    return receiver(factory, Cache(entries=[cached], read_at=WHEN)), presented, app_id


@pytest.mark.usefixtures("schema")
@pytest.mark.parametrize(
    "failure",
    [
        OperationalError("SELECT", {}, errors.AdminShutdown("terminating connection")),
        IntegrityError("SELECT", {}, errors.UniqueViolation("duplicate key value violates")),
        RuntimeError("not a database error"),
    ],
)
async def test_an_internal_error_after_a_match_is_a_500_never_a_200(
    factory: Factory,
    database_url: str,
    captured: StringIO,  # noqa: F811 - fixture
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """The secret matched, the store failed: nothing is durable, so no 200 (PLAN section 4.4)
    but an empty 500 Withings retries, and a warning with the error type and SQLSTATE only. An
    error that only mentions "duplicate" (here a unique violation, SQLSTATE 23505) is an
    error, not a replay."""
    app, presented, _app_id = await linked_receiver(database_url, factory)

    async def fail(*_args: object) -> str:
        raise failure

    monkeypatch.setattr(webhook_receiver, "_ingest", fail)
    broken = await post(app, presented, form())
    assert (broken.status_code, broken.content) == (500, b"")
    logged = captured.getvalue()
    assert logged.count("webhook not stored") == 1
    assert type(failure).__name__ in logged
    assert presented not in logged
    if isinstance(failure, IntegrityError):
        assert "23505" in logged


@pytest.mark.usefixtures("schema")
async def test_a_real_replay_is_matched_by_sqlstate_and_message_and_is_not_an_error(
    factory: Factory,
    database_url: str,
    captured: StringIO,  # noqa: F811 - fixture
) -> None:
    app, presented, _app_id = await linked_receiver(database_url, factory)
    first = await post(app, presented, form())
    replay = await post(app, presented, form())
    forged = await post(app, FORGED, form())
    same_answer(replay, forged)
    same_answer(first, forged)
    with engine_for(database_url) as engine, engine.connect() as admin:
        assert counts(admin) == (1, 1)
    assert "webhook not stored" not in captured.getvalue()


def test_only_the_duplicate_sqlstate_and_message_is_a_replay() -> None:
    other_state = IntegrityError("SELECT", {}, errors.UniqueViolation("duplicate"))
    assert not webhook_receiver._is_duplicate(other_state)  # pyright: ignore[reportPrivateUsage]
    no_diag = OperationalError("SELECT", {}, errors.InvalidParameterValue("duplicate"))
    # A constructed error carries no server diagnostics, so its message is not the server's.
    assert not webhook_receiver._is_duplicate(no_diag)  # pyright: ignore[reportPrivateUsage]
    assert not webhook_receiver._is_duplicate(RuntimeError("duplicate"))  # pyright: ignore[reportPrivateUsage]


class _Diag:
    def __init__(self, message: str) -> None:
        self.message_primary = message


class _ServerError(Exception):
    """A stand-in for a psycopg error with server diagnostics (synthetic)."""

    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.diag = _Diag(message)


@pytest.mark.parametrize(
    ("sqlstate", "message", "replay"),
    [
        ("22023", "duplicate", True),
        ("23505", "duplicate", False),
        ("22023", "bad_dedupe_key", False),
        ("22023", "duplicate key", False),
    ],
)
def test_a_replay_needs_both_the_sqlstate_and_the_server_message(
    sqlstate: str, message: str, replay: bool
) -> None:
    error = OperationalError("SELECT", {}, _ServerError(sqlstate, message))
    assert webhook_receiver._is_duplicate(error) is replay  # pyright: ignore[reportPrivateUsage]


def seed_events(
    admin: Connection, app_id: int, userid: int, count: int, *, age: timedelta, job: bool = True
) -> None:
    """Synthetic job-bearing notifications, ``age`` old, as the superuser."""
    admin.execute(
        text(
            "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, enddate, "
            "dedupe_key, received_at, job_id) "
            "SELECT :app, :user, 1, 1700000000, 1700000000 + g, "
            "'synthetic.' || :tag || '.' || g, now() - CAST(:age AS interval), "
            "CASE WHEN :job THEN gen_random_uuid() END FROM generate_series(1, :n) g"
        ),
        {
            "app": app_id,
            "user": userid,
            "n": count,
            "tag": uuid4().hex,
            "age": f"{int(age.total_seconds())} seconds",
            "job": job,
        },
    )


def stored_job(admin: Connection, app_id: int) -> object:
    """The job id of the one notification the receiver stored (not a seeded row)."""
    return admin.execute(
        text(
            "SELECT job_id FROM webhook_events "
            "WHERE app_id = :app AND dedupe_key NOT LIKE 'synthetic.%'"
        ),
        {"app": app_id},
    ).scalar_one()


@pytest.mark.usefixtures("schema")
@pytest.mark.parametrize(
    ("userid", "count", "age", "job", "elsewhere", "over"),
    [
        (USERID, PER_CONNECTION, timedelta(minutes=5), True, False, True),
        (USERID, PER_CONNECTION - 1, timedelta(minutes=5), True, False, False),
        (USERID, PER_CONNECTION, timedelta(minutes=61), True, False, False),
        (USERID, PER_CONNECTION, timedelta(minutes=5), False, False, False),
        # The same Withings user under another app is another connection.
        (USERID, PER_CONNECTION, timedelta(minutes=5), True, True, False),
        (USERID + 1, PER_APP, timedelta(minutes=5), True, False, True),
        (USERID + 1, PER_APP - 1, timedelta(minutes=5), True, False, False),
        (USERID + 1, PER_APP, timedelta(minutes=5), False, False, False),
        (USERID + 1, PER_APP, timedelta(minutes=61), True, False, False),
        # Another app at its quota does not stop this one.
        (USERID + 1, PER_APP, timedelta(minutes=5), True, True, False),
    ],
)
async def test_the_quotas_record_the_event_and_add_no_job(
    factory: Factory,
    database_url: str,
    captured: StringIO,  # noqa: F811 - fixture
    userid: int,
    count: int,
    age: timedelta,
    job: bool,
    elsewhere: bool,
    over: bool,
) -> None:
    """Per connection (60) and per app (1000) job-bearing events in the last hour. Over quota
    the event is kept without a job and Withings gets the same empty 200."""
    app, presented, app_id = await linked_receiver(database_url, factory)
    with engine_for(database_url) as engine, engine.begin() as admin:
        target = new_app(database_url) if elsewhere else app_id
        seed_events(admin, target, userid, count, age=age, job=job)
    answer = await post(app, presented, form())
    same_answer(answer, await post(app, FORGED, form()))
    with engine_for(database_url) as engine, engine.connect() as admin:
        jobs = admin.execute(text("SELECT count(*) FROM jobs")).scalar_one()
        job_id = stored_job(admin, app_id)
    assert jobs == (0 if over else 1)
    assert (job_id is None) == over
    assert ("webhook over quota" in captured.getvalue()) == over


async def test_append_refuses_an_entry_active_before_every_cache_holds_it() -> None:
    """Refused before a session is opened: the factory is never called."""
    now = datetime(2026, 6, 1, tzinfo=UTC)

    def no_factory() -> None:
        raise AssertionError("append must refuse before touching the database")

    for active_from in (now, now + CACHE_TTL - timedelta(seconds=1), now.replace(tzinfo=None)):
        with pytest.raises(ValueError, match="cache TTL"):
            await append_secret(
                no_factory,  # pyright: ignore[reportArgumentType]
                app_id=1,
                seed=CurrentWebhookSeed(SEED, SEED_BYTES),
                pepper=PEPPER_BYTES,
                pepper_id=PEPPER,
                active_from=active_from,
                now=now,
            )
    with pytest.raises(ValueError, match="cache TTL"):
        await append_secret(
            no_factory,  # pyright: ignore[reportArgumentType]
            app_id=1,
            seed=CurrentWebhookSeed(SEED, SEED_BYTES),
            pepper=PEPPER_BYTES,
            pepper_id=PEPPER,
            active_from=now,
            now=now - CACHE_TTL,
            cache_ttl=CACHE_TTL * 2,
        )


@pytest.mark.usefixtures("schema")
async def test_append_takes_an_entry_exactly_one_cache_ttl_ahead(
    factory: Factory, database_url: str
) -> None:
    seed_keys(database_url)
    app_id = new_app(database_url)
    now = datetime.now(UTC)
    with engine_for(database_url) as engine, engine.begin() as admin:
        admin.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = now(), "
                "rotation_reason = 'routine' WHERE id = :id"
            ),
            {"id": app_id},
        )
    presented = await append_secret(
        factory,
        app_id=app_id,
        seed=CurrentWebhookSeed(SEED, SEED_BYTES),
        pepper=PEPPER_BYTES,
        pepper_id=PEPPER,
        active_from=now + CACHE_TTL,
        now=now,
    )
    assert presented


@pytest.mark.usefixtures("schema")
async def test_a_forged_secret_touches_no_database(
    factory: Factory, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O14: an unknown secret is answered from the in-memory cache, before any session."""
    app, _presented, _app_id = await linked_receiver(database_url, factory)

    def no_session() -> None:
        raise AssertionError("a forged secret must not open a database session")

    monkeypatch.setattr(app.state, "session_factory", no_session)
    answer = await post(app, FORGED, form())
    assert (answer.status_code, answer.content) == (200, b"")
    assert app.state.webhook_metrics.ignored == 1
