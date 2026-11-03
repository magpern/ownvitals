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


# The mutation matrix. Representative owned tables: an ordinary health table, a child whose
# foreign key points at another owned table, a partitioned parent, the parent every health row
# points at, and the append-only events table (restrictive no-update and no-delete policies).
MATRIX_TABLES = ("workouts", "measures", "sleep_series", "connections", "job_events")
OWN = "owner_id = NULLIF(current_setting('app.user_id', true), '')::uuid"
# command -> relaxations: every valid shape of an extra permissive policy wider than the owner
# (USING true, WITH CHECK true, USING own WITH CHECK true, allow-all). SELECT and DELETE take
# no WITH CHECK, INSERT no USING.
RELAXATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "SELECT": (("using-true", "USING (true)"),),
    "INSERT": (("check-true", "WITH CHECK (true)"),),
    "UPDATE": (
        ("using-true", "USING (true)"),
        ("check-true", "WITH CHECK (true)"),
        ("using-own-check-true", f"USING ({OWN}) WITH CHECK (true)"),
        ("allow-all", "USING (true) WITH CHECK (true)"),
    ),
    "DELETE": (("using-true", "USING (true)"),),
    "ALL": (
        ("using-true", "USING (true)"),
        ("check-true", "WITH CHECK (true)"),
        ("using-own-check-true", f"USING ({OWN}) WITH CHECK (true)"),
        ("allow-all", "USING (true) WITH CHECK (true)"),
    ),
}
# A restrictive policy still closes these: job_events is append-only, so an extra UPDATE or
# DELETE policy opens nothing there and the probe must still pass.
NOT_A_HOLE = {("job_events", "UPDATE"), ("job_events", "DELETE")}
MATRIX = [
    pytest.param(table, command, clause, id=f"{table}-{command.lower()}-{label}")
    for table in MATRIX_TABLES
    for command, shapes in RELAXATIONS.items()
    for label, clause in shapes
]


async def test_the_probe_passes_before_any_mutation(database_url: str, factory: Factory) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    async with user_session(factory, make_context(a.user)) as db:
        assert_isolated(await probe(db, a, b, tables), a, tables)


@pytest.mark.parametrize(("table", "command", "clause"), MATRIX)
async def test_the_probe_catches_every_relaxed_policy(
    database_url: str,
    owner_database_url: str,
    factory: Factory,
    table: str,
    command: str,
    clause: str,
) -> None:
    """An extra permissive policy wider than the owner, for one command on one table: the probe
    in a user's session fails on that table (or, where a restrictive policy still closes the
    command, still passes)."""
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    with owner_connection(owner_database_url) as connection:
        connection.execute(
            text(
                f"CREATE POLICY ctx_mutation ON public.{table} AS PERMISSIVE FOR {command} "
                f"TO PUBLIC {clause}"
            )
        )
    try:
        async with user_session(factory, make_context(a.user)) as db:
            report = await probe(db, a, b, tables)
    finally:
        with owner_connection(owner_database_url) as connection:
            connection.execute(text(f"DROP POLICY IF EXISTS ctx_mutation ON public.{table}"))
    if (table, command) in NOT_A_HOLE:
        assert_isolated(report, a, tables)
        return
    with pytest.raises(AssertionError, match=table):
        assert_isolated(report, a, tables)


async def test_the_probe_catches_a_table_without_row_security(
    database_url: str, owner_database_url: str, factory: Factory
) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    with owner_connection(owner_database_url) as connection:
        connection.execute(text("ALTER TABLE public.devices DISABLE ROW LEVEL SECURITY"))
    try:
        async with user_session(factory, make_context(a.user)) as db:
            report = await probe(db, a, b, tables)
    finally:
        with owner_connection(owner_database_url) as connection:
            connection.execute(text("ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY"))
    with pytest.raises(AssertionError, match="devices"):
        assert_isolated(report, a, tables)


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
