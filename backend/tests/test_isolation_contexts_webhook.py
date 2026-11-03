"""RLS in the webhook context (ADR 0002 section 4, PLAN section 4.4; issue #15). Synthetic.

The receiver (``WebhookMiddleware`` on the app role's engine) holds no ``UserContext``: it
validates the notification and calls ``webhook_event_insert``, which resolves
``(app_id, withings_userid)`` to the connection and enqueues the sync job for that connection's
owner. Here the receiver's own session sees and writes no owned row; a notification for one
user makes a job for that user only and answers with no owner or connection id; and the job,
issued when claimed, stays inside its owner's rows. The ids, secret and key bytes are the
synthetic ones of ``test_withings_webhook``.
"""

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import (
    Factory,
    Owner,
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
from jobtools import new_user
from ownvitals.config import Settings
from ownvitals.db import create_engine, create_session_factory
from ownvitals.withings.webhooks import Cache
from test_withings_webhook import WHEN, entry, form, post, receiver
from withingsschema import new_app

KIND = "withings_sync"  # what webhook_event_insert enqueues
USERIDS = (7_100_001, 7_100_002)  # synthetic Withings user ids
UNLINKED = 7_100_009


class SyncInput(BaseModel):
    """The input ``webhook_event_insert`` gives the job (synthetic stand-in for the sync kind)."""

    model_config = ConfigDict(extra="forbid")
    connection_id: UUID
    stream: str


@pytest.fixture
def clean_withings(clean_auth: None, database_url: str) -> None:
    with superuser(database_url) as conn:
        conn.execute(text("TRUNCATE webhook_events, withings_apps CASCADE"))


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def jobs_of(url: str, owner: Owner) -> list[tuple[str, str]]:
    with superuser(url) as conn:
        rows = conn.execute(
            text("SELECT kind, origin FROM jobs WHERE owner_id = :o AND kind = :k ORDER BY 1"),
            {"o": owner.user, "k": KIND},
        )
        return [(str(r.kind), str(r.origin)) for r in rows]


@pytest.mark.usefixtures("clean_withings")
async def test_the_receivers_session_reaches_no_owned_row(
    factory: Factory, database_url: str
) -> None:
    app_id = new_app(database_url)
    a = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[0])
    b = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[1])
    tables = owned_tables(database_url)
    before = snapshot(database_url, (a, b))
    for me, other in ((a, b), (b, a)):
        async with factory() as session, session.begin():  # how the receiver opens its own
            assert_no_context(await probe(session, me, other, tables), tables)
    assert snapshot(database_url, (a, b)) == before


@pytest.mark.usefixtures("clean_withings")
async def test_a_notification_makes_a_job_for_its_owner_only_which_reaches_only_their_rows(
    factory: Factory, database_url: str
) -> None:
    app_id = new_app(database_url)
    a = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[0])
    b = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[1])
    tables = owned_tables(database_url)
    presented, cached = entry(app_id)
    app = receiver(factory, Cache(entries=[cached], read_at=WHEN))
    for me, userid in ((a, USERIDS[0]), (b, USERIDS[1])):
        before = snapshot(database_url, (a, b))
        response = await post(app, presented, form(userid=userid))
        assert response.status_code == 200
        for owner in (a, b):
            for value in (owner.user, owner.connection):
                assert str(value) not in response.text
                assert str(value) not in str(response.headers)
        assert jobs_of(database_url, me) == [(KIND, "webhook")]
        execution, seen = await run_probe_job(factory, KIND, SyncInput, (a, b), tables)
        assert_job_isolated(execution, seen, me, tables)
        after = snapshot(database_url, (a, b))
        assert_unchanged(before, after, changed=(("jobs", me), ("job_events", me)))
    assert jobs_of(database_url, a) == [(KIND, "webhook")]
    assert jobs_of(database_url, b) == [(KIND, "webhook")]


@pytest.mark.usefixtures("clean_withings")
async def test_a_notification_for_an_unlinked_user_makes_no_job(
    factory: Factory, database_url: str
) -> None:
    app_id = new_app(database_url)
    a = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[0])
    b = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[1])
    presented, cached = entry(app_id)
    app = receiver(factory, Cache(entries=[cached], read_at=WHEN))
    before = snapshot(database_url, (a, b))
    response = await post(app, presented, form(userid=UNLINKED))
    assert response.status_code == 200
    assert snapshot(database_url, (a, b)) == before
    assert jobs_of(database_url, a) == jobs_of(database_url, b) == []
