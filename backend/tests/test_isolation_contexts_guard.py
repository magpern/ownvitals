"""The enumeration guard of RLS in every execution context (ADR 0002 section 7, guard "ctx";
issue #15), and proof that the probe of ``contexttools`` catches a missing wall. Synthetic.

Every table in ``public`` with an ``owner_id`` column needs a synthetic row in
``contexttools.ROWS``, and every context of ADR 0002 section 4 a module
``test_isolation_contexts_<context>.py`` that probes every owned table the catalog lists.
"""

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
    Uncovered,
    assert_isolated,
    owned_tables,
    plant,
    probe,
    uncovered,
)
from ctx import make_context
from dbtools import drop_tables, owner_connection, run_rls_helper
from jobtools import new_user
from ownvitals.config import Settings
from ownvitals.db import create_engine, create_session_factory, user_session

TESTS = Path(__file__).resolve().parent
PLANTED = "t_ctx_uncovered"
pytestmark = pytest.mark.usefixtures("clean_auth")


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = create_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
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
    database_url: str, planted_table: str, factory: Factory
) -> None:
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    async with user_session(factory, make_context(a.user)) as db:
        with pytest.raises(Uncovered, match=planted_table):
            await probe(db, a, b, owned_tables(database_url))


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


async def test_the_probe_catches_a_table_without_row_security(
    database_url: str, owner_database_url: str, factory: Factory
) -> None:
    """With one table's row security turned off, the same probe in a user's session fails."""
    a, b = plant(database_url, new_user()), plant(database_url, new_user())
    tables = owned_tables(database_url)
    async with user_session(factory, make_context(a.user)) as db:
        assert_isolated(await probe(db, a, b, tables), a, tables)
    with owner_connection(owner_database_url) as connection:
        connection.execute(text("ALTER TABLE public.devices DISABLE ROW LEVEL SECURITY"))
    try:
        async with user_session(factory, make_context(a.user)) as db:
            report = await probe(db, a, b, tables)
        with pytest.raises(AssertionError, match="devices"):
            assert_isolated(report, a, tables)
    finally:
        with owner_connection(owner_database_url) as connection:
            connection.execute(text("ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY"))


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
