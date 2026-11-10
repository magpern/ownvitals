"""RLS in the maintenance context (ADR 0002 section 4: "Maintenance (reap, purge)"; issue #15).

Maintenance holds no ``UserContext``: it runs definer functions in a ``system_session`` (the
reaper, ``queue_reap``) or a definer function keyed on the session's own user (the per-owner
purge, ``webhook_events_purge_owner``, which reads the owner from ``app.user_id`` and never from
an argument). Here its session sees and writes no owned row; reaping one user's expired job
changes that job and nothing else of either user; and the purge removes only the session user's
notifications, and nothing at all without a user. Synthetic ids and rows only.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import (
    LEASE_SECONDS,
    Factory,
    assert_no_context,
    assert_unchanged,
    owned_tables,
    plant,
    probe,
    snapshot,
    superuser,
)
from ctx import make_context
from jobcore import settings_for
from jobtools import new_job, new_user
from ownvitals.db import create_session_factory, system_session, user_session
from ownvitals.jobs import queue
from ownvitals.worker.loop import create_worker_engine
from withingsschema import new_app

KIND = "ctx_probe_reaped"
USERIDS = (7_200_001, 7_200_002)  # synthetic Withings user ids
pytestmark = pytest.mark.usefixtures("clean_auth")


@pytest.fixture
def clean_withings(clean_auth: None, database_url: str) -> None:
    with superuser(database_url) as conn:
        conn.execute(text("TRUNCATE webhook_events, withings_apps CASCADE"))


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_worker_engine(settings_for(app_database_url), pool_size=4)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def test_the_maintenance_session_reaches_no_owned_row(
    factory: Factory, database_url: str
) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    before = snapshot(database_url, (a, b))
    for me, other in ((a, b), (b, a)):
        async with system_session(factory) as db:
            assert_no_context(await probe(db, me, other, tables), tables)
    assert snapshot(database_url, (a, b)) == before


async def test_reaping_one_users_job_touches_nothing_else(
    factory: Factory, database_url: str
) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    with superuser(database_url) as conn:
        job_id = new_job(conn, a.user, kind=KIND)
    claimed = await queue.claim(factory, "ctx-reaped-worker", LEASE_SECONDS, [KIND])
    assert claimed is not None
    assert (claimed.job_id, claimed.owner_id) == (job_id, a.user)
    with superuser(database_url) as conn:
        conn.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )
    before = snapshot(database_url, (a, b))
    assert await queue.reap(factory) == 1
    after = snapshot(database_url, (a, b))
    assert_unchanged(before, after, changed=(("job_events", a),))
    with superuser(database_url) as conn:
        state = conn.execute(text("SELECT state FROM jobs WHERE id = :id"), {"id": job_id})
        assert state.scalar_one() != "running"


def _events(url: str) -> set[int]:
    with superuser(url) as conn:
        rows = conn.execute(text("SELECT withings_userid FROM webhook_events"))
        return {int(v) for v in rows.scalars()}


@pytest.mark.usefixtures("clean_withings")
async def test_the_owner_purge_removes_only_the_session_users_notifications(
    factory: Factory, database_url: str
) -> None:
    app_id = new_app(database_url)
    a = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[0])
    b = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[1])
    with superuser(database_url) as conn:
        for userid in USERIDS:
            conn.execute(
                text(
                    "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, "
                    "enddate, dedupe_key) VALUES (:a, :u, 1, 1, 2, :k)"
                ),
                {"a": app_id, "u": userid, "k": f"synthetic-ctx-purge-{userid}"},
            )
    before = snapshot(database_url, (a, b))
    purge = text("SELECT public.webhook_events_purge_owner()")
    with pytest.raises(DBAPIError, match="bad_owner"):
        async with system_session(factory) as db:
            await db.execute(purge)
    assert _events(database_url) == set(USERIDS)
    async with user_session(factory, make_context(a.user)) as db:
        await db.execute(purge)
    assert _events(database_url) == {USERIDS[1]}
    assert snapshot(database_url, (a, b)) == before


@pytest.mark.usefixtures("clean_withings")
async def test_the_account_purge_runs_only_in_a_system_session_and_only_for_its_owner(
    factory: Factory, database_url: str
) -> None:
    """#362: the admin delete's ``webhook_events_purge_account(p_owner)``. The owner is an
    argument, so it refuses inside any user session (even the owner's own) and deletes nothing
    there; in a system session it removes only ``p_owner``'s notifications."""
    app_id = new_app(database_url)
    a = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[0])
    b = plant(database_url, new_user(), app_id=app_id, withings_userid=USERIDS[1])
    with superuser(database_url) as conn:
        for userid in USERIDS:
            conn.execute(
                text(
                    "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, "
                    "enddate, dedupe_key) VALUES (:a, :u, 1, 1, 2, :k)"
                ),
                {"a": app_id, "u": userid, "k": f"synthetic-ctx-account-{userid}"},
            )
    before = snapshot(database_url, (a, b))
    purge = text("SELECT public.webhook_events_purge_account(:o)")
    for session_user in (a.user, b.user):
        with pytest.raises(DBAPIError, match="bad_context"):
            async with user_session(factory, make_context(session_user)) as db:
                await db.execute(purge, {"o": a.user})
    assert _events(database_url) == set(USERIDS)
    with pytest.raises(DBAPIError, match="bad_owner"):
        async with system_session(factory) as db:
            await db.execute(purge, {"o": None})
    assert _events(database_url) == set(USERIDS)
    async with system_session(factory) as db:
        await db.execute(purge, {"o": a.user})
    assert _events(database_url) == {USERIDS[1]}
    assert snapshot(database_url, (a, b)) == before
