"""The enumeration guard of RLS in every execution context (ADR 0002 section 7, guard "ctx";
issue #15), and proof that the probe of ``contexttools`` catches a missing wall. Synthetic.

Every table in ``public`` with an ``owner_id`` column needs a synthetic row in
``contexttools.ROWS``, and every context of ADR 0002 section 4 a module
``test_isolation_contexts_<context>.py`` that probes every owned table the catalog lists.
"""

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import (
    CONTEXTS,
    ROWS,
    Factory,
    Target,
    Uncovered,
    admin_probe,
    assert_isolated,
    assert_job_isolated,
    owned_tables,
    plant,
    probe,
    run_probe_job,
    superuser,
    uncovered,
)
from ctx import make_context
from dbtools import drop_tables, owner_connection, run_rls_helper
from jobcore import settings_for
from jobtools import new_job, new_user
from ownvitals.config import Settings
from ownvitals.db import create_engine, create_session_factory, system_session, user_session
from ownvitals.worker.loop import create_worker_engine

TESTS = Path(__file__).resolve().parent
PLANTED = "t_ctx_uncovered"
GUARD_KIND = "ctx_probe_guard"
pytestmark = pytest.mark.usefixtures("clean_auth")


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def worker_factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_worker_engine(settings_for(app_database_url), pool_size=4)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def planted_table(owner_database_url: str) -> Iterator[str]:
    """An owned table, made as a migration would make one, that ``ROWS`` does not know."""
    drop_tables(owner_database_url, PLANTED)
    with owner_connection(owner_database_url) as connection:
        connection.execute(
            text(
                f"CREATE TABLE public.{PLANTED} (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
                "owner_id uuid NOT NULL, UNIQUE (owner_id, id))"
            )
        )
        run_rls_helper(connection, PLANTED)
    try:
        yield PLANTED
    finally:
        drop_tables(owner_database_url, PLANTED)


def test_every_owned_table_has_a_synthetic_row(database_url: str) -> None:
    missing, stale = uncovered(owned_tables(database_url))
    assert missing == [], f"owned tables without a context test (add them to ROWS): {missing}"
    assert stale == [], f"ROWS entries for tables that are not owned: {stale}"


def test_the_guard_fails_on_a_planted_owned_table_without_coverage(
    database_url: str, planted_table: str
) -> None:
    missing, stale = uncovered(owned_tables(database_url))
    assert (missing, stale) == ([planted_table], [])


async def test_every_context_fails_on_a_planted_owned_table(
    database_url: str, planted_table: str, factory: Factory, worker_factory: Factory
) -> None:
    """Each way the context modules reach owned rows refuses a catalog with an uncovered table:
    a user session (web), a session without a user (scheduler, maintenance) and the receiver's
    plain session (webhook), a claimed job (worker, scheduler and webhook jobs) and the admin
    probe (admin; run here as the superuser, since only its coverage check is under test)."""
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    assert planted_table in tables
    async with user_session(factory, make_context(a.user)) as db:
        with pytest.raises(Uncovered, match=planted_table):
            await probe(db, a, b, tables)
    async with system_session(factory) as db:
        with pytest.raises(Uncovered, match=planted_table):
            await probe(db, a, b, tables)
    async with factory() as session, session.begin():
        with pytest.raises(Uncovered, match=planted_table):
            await probe(session, a, b, tables)
    with superuser(database_url) as conn:
        new_job(conn, a.user, kind=GUARD_KIND, input=json.dumps(b.as_json()))
    execution, seen = await run_probe_job(worker_factory, GUARD_KIND, Target, (a, b), tables)
    assert seen.ctx is not None
    assert seen.report is None
    assert execution.result != "completed"
    with pytest.raises(AssertionError):
        assert_job_isolated(execution, seen, a, tables)
    engine = create_async_engine(database_url)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            with pytest.raises(Uncovered, match=planted_table):
                await admin_probe(session, (a, b), tables)
    finally:
        await engine.dispose()


def test_the_guard_fails_on_a_stale_row(database_url: str) -> None:
    found = owned_tables(database_url) - {"devices"}
    assert uncovered(found) == ([], ["devices"])


def test_every_context_has_a_module_that_probes_the_catalog() -> None:
    for context in CONTEXTS:
        module = TESTS / f"test_isolation_contexts_{context}.py"
        assert module.is_file(), module.name
        source = module.read_text(encoding="utf-8")
        assert "owned_tables(" in source, module.name
        assert "probe" in source, module.name


@pytest.mark.parametrize(
    ("table", "mutation", "undo"),
    [
        (
            "devices",
            "ALTER TABLE public.devices DISABLE ROW LEVEL SECURITY",
            "ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY",
        ),
        # Write-only holes: the read policy still holds, so only the probe's writes that read
        # no column (blind_update, blind_delete) can see them.
        (
            "workouts",
            "CREATE POLICY ctx_mutation ON public.workouts AS PERMISSIVE FOR UPDATE TO PUBLIC "
            "USING (true) WITH CHECK (true)",
            "DROP POLICY IF EXISTS ctx_mutation ON public.workouts",
        ),
        (
            "data_revisions",
            "CREATE POLICY ctx_mutation ON public.data_revisions AS PERMISSIVE FOR DELETE "
            "TO PUBLIC USING (true)",
            "DROP POLICY IF EXISTS ctx_mutation ON public.data_revisions",
        ),
        # Insert-only hole: the probe's inserts return nothing, so the read policy can't mask it.
        (
            "ecg_recordings",
            "CREATE POLICY ctx_mutation ON public.ecg_recordings AS PERMISSIVE FOR INSERT "
            "TO PUBLIC WITH CHECK (true)",
            "DROP POLICY IF EXISTS ctx_mutation ON public.ecg_recordings",
        ),
    ],
)
async def test_the_probe_catches_a_hole_in_one_tables_row_security(
    database_url: str,
    owner_database_url: str,
    factory: Factory,
    table: str,
    mutation: str,
    undo: str,
) -> None:
    """With one table's row security broken (off, or an extra allow-all write or insert
    policy), the same probe in a user's session fails on that table."""
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    async with user_session(factory, make_context(a.user)) as db:
        assert_isolated(await probe(db, a, b, tables), a, tables)
    with owner_connection(owner_database_url) as connection:
        connection.execute(text(mutation))
    try:
        async with user_session(factory, make_context(a.user)) as db:
            report = await probe(db, a, b, tables)
        with pytest.raises(AssertionError, match=table):
            assert_isolated(report, a, tables)
    finally:
        with owner_connection(owner_database_url) as connection:
            connection.execute(text(undo))


async def test_the_probe_catches_a_session_that_bypasses_row_security(
    database_url: str,
) -> None:
    """The superuser bypasses row security: the probe, run as it, sees the other user."""
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    engine = create_async_engine(database_url)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            report = await probe(session, a, b, tables)
    finally:
        await engine.dispose()
    assert set(report) == set(ROWS)
    with pytest.raises(AssertionError):
        assert_isolated(report, a, tables)
    assert all(sorted(row["seen"]) == sorted({str(a.user), str(b.user)}) for row in report.values())
