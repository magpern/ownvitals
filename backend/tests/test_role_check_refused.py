"""Guard 6 of ADR 0002 section 7, continued (#89): a login the database refuses stops startup.

Postgres refuses some logins outright at connect: a wrong password, a role that may not log in,
no CONNECT privilege, or a connection option the role may not set. A non-superuser login with
``-c session_authorization=...`` in ``options`` or ``PGOPTIONS`` (the #81 review's N3) gets
``FATAL: permission denied to set session authorization`` (SQLSTATE 42501). Each of these is a
configuration error: web, the queue check and the key ring refuse to start (``DatabaseRoleError``,
or the key ring's ``OperationalError``) instead of reading it as a database that is down. Only a
real connection failure (refused, a timeout, DNS, a server that is starting up) keeps the
"unreachable, checked on first connection" path."""

import secrets
from collections.abc import Callable, Iterator

import psycopg
import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from dbtools import url_for
from keystools import app_factory, settings_for
from ownvitals.crypto import keks
from ownvitals.db import DatabaseRoleError, create_engine, verify_database_role
from ownvitals.db.role_check import is_authentication_failure
from ownvitals.web import create_app

PASSWORD = secrets.token_hex(8)
LOGIN = "guard_refused"
APP_ROLE = "ownvitals_app"
REFUSED = "refused the configured credentials"
SESSION_AUTHORIZATION = f"-c session_authorization={APP_ROLE}"  # the review's exact options


def admin_run(database_url: str, *statements: str) -> None:
    engine = create_sync_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def drop_login(database_url: str) -> None:
    db = make_url(database_url).database
    engine = create_sync_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            found = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": LOGIN}
            )
            if found.first():
                connection.execute(text(f"REVOKE CONNECT ON DATABASE {db} FROM {LOGIN}"))
                connection.execute(text(f"DROP OWNED BY {LOGIN}"))
                connection.execute(text(f"DROP ROLE {LOGIN}"))
    finally:
        engine.dispose()


@pytest.fixture
def plain_login(database_url: str) -> Iterator[str]:
    """A plain login role (no membership, the three timeouts, CONNECT); its URL."""
    db = make_url(database_url).database
    drop_login(database_url)
    admin_run(
        database_url,
        f"CREATE ROLE {LOGIN} LOGIN PASSWORD '{PASSWORD}'",
        f"GRANT CONNECT ON DATABASE {db} TO {LOGIN}",
        f"ALTER ROLE {LOGIN} SET statement_timeout = '30s'",
        f"ALTER ROLE {LOGIN} SET lock_timeout = '5s'",
        f"ALTER ROLE {LOGIN} SET idle_in_transaction_session_timeout = '60s'",
    )
    try:
        yield url_for(database_url, LOGIN, PASSWORD)
    finally:
        drop_login(database_url)


def with_options(url: str, options: str) -> str:
    return make_url(url).update_query_dict({"options": options}).render_as_string(False)


def no_options(url: str) -> str:
    return url


def authorization_option(url: str) -> str:
    return with_options(url, SESSION_AUTHORIZATION)


def role_option(url: str) -> str:
    return with_options(url, f"-c role={APP_ROLE}")


def wrong_password(url: str) -> str:
    return url_for(url, LOGIN, "wrong-" + PASSWORD)


# How each login is refused: statements as the superuser, the URL made from the plain login's,
# and PGOPTIONS (or None).
REFUSALS: dict[str, tuple[tuple[str, ...], Callable[[str], str], str | None]] = {
    "session_authorization in options": ((), authorization_option, None),
    "session_authorization in PGOPTIONS": ((), no_options, SESSION_AUTHORIZATION),
    "role in options, not a member": ((), role_option, None),
    "no CONNECT privilege": (("REVOKE CONNECT ON DATABASE {db} FROM {login}",), no_options, None),
    "a role that may not log in": (("ALTER ROLE {login} NOLOGIN",), no_options, None),
    "a wrong password": ((), wrong_password, None),
}


@pytest.fixture(params=list(REFUSALS.values()), ids=list(REFUSALS))
def refused_url(
    request: pytest.FixtureRequest,
    plain_login: str,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """A URL whose login the server refuses at connect, in each of the ways above."""
    statements, make, pgoptions = request.param
    db = make_url(database_url).database
    admin_run(database_url, *(s.format(db=db, login=LOGIN) for s in statements))
    if pgoptions is not None:
        monkeypatch.setenv("PGOPTIONS", pgoptions)
    return make(plain_login)


async def test_the_plain_login_itself_starts(plain_login: str) -> None:
    """The refusals below come from what each case changes, not from the login."""
    engine = create_engine(settings_for(plain_login))
    try:
        await verify_database_role(engine)
    finally:
        await engine.dispose()


async def test_a_refused_login_stops_startup_with_a_database_role_error(refused_url: str) -> None:
    engine = create_engine(settings_for(refused_url))
    try:
        with pytest.raises(DatabaseRoleError, match=REFUSED) as err:
            await verify_database_role(engine)
    finally:
        await engine.dispose()
    assert PASSWORD not in str(err.value)
    assert LOGIN not in str(err.value)
    app = create_app(settings_for(refused_url))
    with pytest.raises(DatabaseRoleError, match=REFUSED):
        async with app.router.lifespan_context(app):
            pytest.fail("web must not start")


async def test_a_refused_login_during_the_key_ring_check_refuses_rather_than_defers(
    refused_url: str,
) -> None:
    """``load_keyring`` defers its check only for a database unreachable at connect (#82 review,
    non-blocking 2): a refused login propagates, the worker's start included."""
    for role in ("web", "worker"):
        async with app_factory(settings_for(refused_url)) as factory:
            with pytest.raises(OperationalError) as err:
                await keks.load_keyring(factory, settings_for(refused_url), role=role)
        assert is_authentication_failure(err.value)


def failure(message: str, sqlstate: str | None = None) -> OperationalError:
    """An ``OperationalError`` as SQLAlchemy wraps a psycopg error at connect."""
    orig: Exception = psycopg.OperationalError(message)
    if sqlstate is not None:
        orig = type("Diagnosed", (Exception,), {"sqlstate": sqlstate})(message)
    return OperationalError("connect", None, orig)


AT = 'connection failed: connection to server at "127.0.0.1", port 5432 failed: '
# Messages as libpq reports them at connect (psycopg gives no SQLSTATE for these).
REFUSED_MESSAGES = {
    "session_authorization": AT + 'FATAL:  permission denied to set session authorization "x"',
    "role": AT + 'FATAL:  permission denied to set role "ownvitals_app"',
    "no CONNECT": AT + 'FATAL:  permission denied for database "ownvitals"\n'
    "DETAIL:  User does not have CONNECT privilege.",
    "a parameter": AT + 'FATAL:  permission denied to set parameter "log_statement"',
    "password": AT + 'FATAL:  password authentication failed for user "x"',
    "no such role": AT + 'FATAL:  role "x" does not exist',
    "NOLOGIN": AT + 'FATAL:  role "x" is not permitted to log in',
    "pg_hba.conf": AT + 'FATAL:  no pg_hba.conf entry for host "127.0.0.1", user "x"',
    "pg_hba.conf reject": AT + 'FATAL:  pg_hba.conf rejects connection for host "127.0.0.1"',
    "no password": AT + "fe_sendauth: no password supplied",
}
UNREACHABLE_MESSAGES = {
    "connection refused": AT + "Connection refused (0x0000274D/10061)",
    "timeout": "connection timeout expired",
    "DNS": 'failed to resolve host "db.invalid": [Errno 11001] getaddrinfo failed',
    "starting up": AT + "FATAL:  the database system is starting up",
    "shutting down": AT + "FATAL:  the database system is shutting down",
    "too many clients": AT + "FATAL:  sorry, too many clients already",
    "no such database": AT + 'FATAL:  database "ownvitals" does not exist',
    "a socket's permissions": 'connection to server on socket "/run/postgresql/.s.PGSQL.5432" '
    "failed: Permission denied",
}


@pytest.mark.parametrize("message", REFUSED_MESSAGES.values(), ids=REFUSED_MESSAGES.keys())
def test_every_login_the_server_refuses_is_classified_as_refused(message: str) -> None:
    assert is_authentication_failure(failure(message))


@pytest.mark.parametrize("sqlstate", ["28000", "28P01", "42501"])
def test_a_refusal_sqlstate_is_classified_as_refused(sqlstate: str) -> None:
    assert is_authentication_failure(failure("refused", sqlstate))


@pytest.mark.parametrize("message", UNREACHABLE_MESSAGES.values(), ids=UNREACHABLE_MESSAGES.keys())
def test_a_real_connection_failure_stays_unreachable(message: str) -> None:
    assert not is_authentication_failure(failure(message))
    assert not is_authentication_failure(failure(message, "08006"))
