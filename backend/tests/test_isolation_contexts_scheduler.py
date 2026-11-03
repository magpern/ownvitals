"""RLS in the scheduler context (ADR 0002 section 4; issue #15). Synthetic users and rows.

The scheduler holds no ``UserContext``: it reaches owned rows only through ``queue_enqueue``
(``ownvitals.jobs.queue.enqueue``, in a ``system_session``), which takes a connection id, never an
owner, and makes the job the connection's owner's. Here the scheduler's own session sees and
writes no owned row for anyone; the job it enqueued gets its context only when claimed, from the
job issuer, and stays inside its owner's rows although its input names the other user.
"""

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import (
    Factory,
    Owner,
    Target,
    assert_job_isolated,
    assert_no_context,
    assert_unchanged,
    owned_tables,
    plant,
    probe,
    run_probe_job,
    snapshot,
    superuser,
)
from jobcore import settings_for
from jobtools import new_user
from ownvitals.db import create_session_factory, system_session
from ownvitals.jobs import queue
from ownvitals.worker.loop import create_worker_engine

KIND = "ctx_probe_scheduled"
pytestmark = pytest.mark.usefixtures("clean_auth")


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_worker_engine(settings_for(app_database_url), pool_size=4)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def schedule(factory: Factory, connection: object, target: Owner) -> object:
    """What the scheduler does for a due connection (PLAN section 4.4)."""
    return await queue.enqueue(
        factory,
        connection_id=connection,  # pyright: ignore[reportArgumentType]
        kind=KIND,
        origin="scheduler",
        payload=json.dumps(target.as_json()),
        singleton_key=None,
        max_attempts=1,
        target_secret_id=None,
    )


async def test_the_schedulers_session_reaches_no_owned_row(
    factory: Factory, database_url: str
) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    before = snapshot(database_url, (a, b))
    for me, other in ((a, b), (b, a)):
        async with system_session(factory) as db:
            assert_no_context(await probe(db, me, other, tables), tables)
    assert snapshot(database_url, (a, b)) == before


async def test_a_scheduled_job_is_its_connections_owners_and_reaches_only_their_rows(
    factory: Factory, database_url: str
) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    for me, other in ((a, b), (b, a)):
        before = snapshot(database_url, (a, b))
        job_id = await schedule(factory, me.connection, other)
        with superuser(database_url) as conn:
            row = conn.execute(
                text("SELECT owner_id, origin FROM jobs WHERE id = :id"), {"id": job_id}
            ).one()
        assert (row.owner_id, row.origin) == (me.user, "scheduler")
        execution, seen = await run_probe_job(factory, KIND, Target, (a, b), tables)
        assert_job_isolated(execution, seen, me, tables)
        after = snapshot(database_url, (a, b))
        assert_unchanged(before, after, changed=(("jobs", me), ("job_events", me)))


async def test_the_scheduler_cannot_enqueue_for_an_unknown_or_disconnected_connection(
    factory: Factory, database_url: str
) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    with superuser(database_url) as conn:
        disconnected = conn.execute(
            text("SELECT id FROM connections WHERE owner_id = :o AND status = 'disconnected'"),
            {"o": a.user},
        ).scalar_one()
    before = snapshot(database_url, (a, b))
    for connection in (uuid4(), disconnected):
        with pytest.raises(DBAPIError, match="unknown connection"):
            await schedule(factory, connection, b)
    assert snapshot(database_url, (a, b)) == before
