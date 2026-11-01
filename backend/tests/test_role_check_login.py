"""Guard 6 of ADR 0002 section 7, continued: the check runs on the **login** role, and a session
that starts with ``app.user_id`` set is refused with a ``DatabaseRoleError`` (#75).

A login role can start as another role: a default ``role`` setting (``ALTER ROLE ... SET role``)
or the connection options (``options=-c role=...``) make ``current_user`` differ from
``session_user``. A superuser login, or a member of ``ownvitals_owner``, that starts as
``ownvitals_app`` can ``SET ROLE NONE`` or ``SET ROLE ownvitals_owner`` and bypass RLS at any
time, so checking only ``current_user`` would let it through."""

import asyncio
import secrets
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from dbtools import url_for
from ownvitals.config import Settings
from ownvitals.db import DatabaseRoleError, create_engine, verify_database_role

PASSWORD = secrets.token_hex(8)
LOGIN = "guard_login"
APP_ROLE = "ownvitals_app"
SYNTHETIC_USER = "00000000-0000-4000-8000-000000000001"  # synthetic: no real user

SUPERUSER = "is a superuser or a member of one"
MEMBER = "is a member of another role"
SWITCH = "logs in as one role and runs as another"
SETTINGS = "has role settings other than the expected three timeouts"
LIVE_USER = "starts each session with app.user_id set"
OWNS = ("owns tables, views or materialized views", "owns the database", "owns a schema")
TIMEOUTS = (
    f"ALTER ROLE {LOGIN} SET statement_timeout = '30s'",
    f"ALTER ROLE {LOGIN} SET lock_timeout = '5s'",
    f"ALTER ROLE {LOGIN} SET idle_in_transaction_session_timeout = '60s'",
)


def admin_run(database_url: str, *statements: str) -> None:
    engine = create_sync_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def drop_login(database_url: str) -> None:
    engine = create_sync_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            found = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": LOGIN}
            )
            if found.first():
                connection.execute(text(f"DROP OWNED BY {LOGIN}"))
                connection.execute(text(f"DROP ROLE {LOGIN}"))
    finally:
        engine.dispose()


def with_options(url: str, options: str) -> str:
    """``url`` with libpq connection options (``-c name=value``) in its query."""
    return make_url(url).update_query_dict({"options": options}).render_as_string(False)


def session_and_current_user(url: str) -> tuple[str, str]:
    """Who a plain, unchecked connection of ``url`` logs in as and runs as."""
    engine = create_sync_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT session_user, current_user")).one()
            return str(row[0]), str(row[1])
    finally:
        engine.dispose()


async def assert_refused(url: str, problems: list[str], absent: tuple[str, ...] = ()) -> None:
    """Startup and a new pooled connection both raise ``DatabaseRoleError`` naming ``problems``
    (and none of ``absent``), never another error and never the password."""
    settings = Settings(database_url=url, environment="test")  # pyright: ignore[reportArgumentType]
    engine = create_engine(settings)
    try:
        with pytest.raises(DatabaseRoleError) as startup:
            await verify_database_role(engine)
        with pytest.raises(DatabaseRoleError) as pooled:
            async with engine.connect() as connection:  # the per-connection check
                await connection.execute(text("SELECT 1"))
        for error in (startup.value, pooled.value):
            assert all(problem in str(error) for problem in problems), str(error)
            assert not any(problem in str(error) for problem in absent), str(error)
            assert PASSWORD not in str(error)
    finally:
        await engine.dispose()


async def assert_accepted(url: str) -> None:
    settings = Settings(database_url=url, environment="test")  # pyright: ignore[reportArgumentType]
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


@pytest.fixture
def login_role(database_url: str) -> Iterator[None]:
    drop_login(database_url)
    try:
        yield
    finally:
        drop_login(database_url)


# Login roles that start as the app role. Each one is planted with the three timeouts, so the
# checkout check alone would not refuse it.
LOGINS = {
    "a superuser": (
        [f"CREATE ROLE {LOGIN} LOGIN SUPERUSER PASSWORD '{PASSWORD}'"],
        [SUPERUSER, MEMBER],
    ),
    "a NOINHERIT member of the owner": (
        [
            f"CREATE ROLE {LOGIN} LOGIN NOINHERIT PASSWORD '{PASSWORD}'",
            f"GRANT ownvitals_owner TO {LOGIN} WITH INHERIT FALSE, SET TRUE",
            f"GRANT {APP_ROLE} TO {LOGIN} WITH INHERIT FALSE, SET TRUE",
            f"GRANT CONNECT ON DATABASE {{db}} TO {LOGIN}",
        ],
        [MEMBER, *OWNS],
    ),
}


@pytest.mark.parametrize(("plant", "problems"), LOGINS.values(), ids=LOGINS.keys())
@pytest.mark.parametrize("switch", ["role default", "connection option"])
async def test_a_login_role_that_starts_as_the_app_role_is_refused(
    database_url: str,
    login_role: None,
    owned_tables: None,
    plant: list[str],
    problems: list[str],
    switch: str,
) -> None:
    db = make_url(database_url).database
    admin_run(database_url, *(s.format(db=db) for s in plant), *TIMEOUTS)
    url = url_for(database_url, LOGIN, PASSWORD)
    if switch == "role default":
        admin_run(database_url, f"ALTER ROLE {LOGIN} SET role = '{APP_ROLE}'")
        expected, absent = [*problems, SWITCH, SETTINGS], ()
    else:
        url = with_options(url, f"-c role={APP_ROLE}")
        expected, absent = [*problems, SWITCH], (SETTINGS,)
    # The plant works: the session runs as the app role, the role the old check looked at.
    assert session_and_current_user(url) == (LOGIN, APP_ROLE)
    await assert_refused(url, expected, absent)


# app.user_id set outside the catalog: the connect check reads the live value, so startup fails
# with a DatabaseRoleError instead of the checkout check replacing the connection until the pool
# gives up ("This connection is closed").
async def test_app_user_id_in_the_connection_options_is_refused(app_database_url: str) -> None:
    url = with_options(app_database_url, f"-c app.user_id={SYNTHETIC_USER}")
    await assert_refused(url, [LIVE_USER])
    await assert_accepted(app_database_url)


async def test_app_user_id_in_pgoptions_is_refused(app_database_url: str) -> None:
    with pytest.MonkeyPatch.context() as env:
        env.setenv("PGOPTIONS", f"-c app.user_id={SYNTHETIC_USER}")
        await assert_refused(app_database_url, [LIVE_USER])
    await assert_accepted(app_database_url)


def live_user_id(database_url: str) -> str:
    """``app.user_id`` as a brand-new session sees it."""
    engine = create_sync_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            value = connection.execute(text("SELECT current_setting('app.user_id', true)"))
            return value.scalar_one() or ""
    finally:
        engine.dispose()


async def reload_until(database_url: str, expected: str) -> None:
    """Reload the server configuration and wait until new sessions see ``expected``: the
    postmaster applies a reload asynchronously."""
    admin_run(database_url, "SELECT pg_reload_conf()")
    deadline = time.monotonic() + 10
    while live_user_id(database_url) != expected:
        assert time.monotonic() < deadline, "the server did not apply the configuration reload"
        await asyncio.sleep(0.05)


async def test_app_user_id_set_server_wide_is_refused(
    database_url: str, app_database_url: str
) -> None:
    admin_run(database_url, f"ALTER SYSTEM SET app.user_id = '{SYNTHETIC_USER}'")
    try:
        await reload_until(database_url, SYNTHETIC_USER)
        await assert_refused(app_database_url, [LIVE_USER])
    finally:
        admin_run(database_url, "ALTER SYSTEM RESET app.user_id")
        await reload_until(database_url, "")
    await assert_accepted(app_database_url)
