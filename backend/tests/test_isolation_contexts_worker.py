"""RLS in the worker-job context (ADR 0002 section 4, ADR 0005 section 4; issue #15). Synthetic.

A job a user created (``origin = 'user'``, as the jobs API makes it) whose input names the other
user's rows. The worker claims it and runs it the way it runs every job (``runner.execute``): the
job issuer builds the context from the claimed row alone, with the worker's account directory,
and the handler writes only through the fence. The probe in its fenced transaction stays inside
the job owner's rows.
"""

import json
from collections.abc import AsyncIterator

import pytest

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import (
    Factory,
    Owner,
    Target,
    assert_job_isolated,
    assert_unchanged,
    owned_tables,
    plant,
    run_probe_job,
    snapshot,
    superuser,
)
from jobcore import settings_for
from jobtools import new_job, new_user
from ownvitals.db import create_session_factory
from ownvitals.worker.loop import create_worker_engine

KIND = "ctx_probe_user_job"
pytestmark = pytest.mark.usefixtures("clean_auth")


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_worker_engine(settings_for(app_database_url), pool_size=4)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def user_job(url: str, owner: Owner, target: Owner) -> None:
    with superuser(url) as conn:
        new_job(conn, owner.user, kind=KIND, input=json.dumps(target.as_json()))


async def test_a_worker_job_reaches_only_its_owners_rows(
    factory: Factory, database_url: str
) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    for me, other in ((a, b), (b, a)):
        before = snapshot(database_url, (a, b))
        user_job(database_url, me, other)
        execution, seen = await run_probe_job(factory, KIND, Target, (a, b), tables)
        assert_job_isolated(execution, seen, me, tables)
        after = snapshot(database_url, (a, b))
        assert_unchanged(before, after, changed=(("jobs", me), ("job_events", me)))
