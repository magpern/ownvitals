"""Helpers for the tests that need the throwaway Postgres (``OWNVITALS_TEST_DATABASE_URL``) and
create tables as the owner role, the way migrations do."""

import ipaddress
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import pytest
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

from ownvitals.db.rls import enable_owner_rls

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROLES_SCRIPT = BACKEND_DIR.parent / "docker" / "postgres" / "init" / "10-roles.sql"
# libpq reads these when the URL leaves the host or a service open; the database fixtures drop
# them for the test process, so libpq connects to exactly the host the loopback check saw.
LIBPQ_HOST_ENVIRONMENT = ("PGHOST", "PGHOSTADDR", "PGSERVICE")


def unavailable(reason: str) -> NoReturn:
    """The test database is missing: fail in CI (``CI=true``), skip anywhere else, so the
    database tests can never be skipped silently where they must run."""
    if os.environ.get("CI") == "true":
        pytest.fail(reason + "; database tests must run in CI")
    pytest.skip(reason)


def _is_local_host(host: str) -> bool:
    """Loopback by name or address, or a Unix socket directory. An empty host is not: libpq
    would fill it in from ``PGHOST`` or its compiled-in default, which this check never sees."""
    if host == "localhost" or host.startswith("/"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def require_loopback_database(url: str) -> None:
    """Fail unless every host ``url`` can connect to is loopback or a local socket.

    The network guard patches Python's sockets, but libpq opens its own: a test database URL
    pointing elsewhere would connect (and send test data) unseen. ``host`` and ``hostaddr``
    may also come in the query string, and a host can be a comma-separated list. A URL without
    any host, an empty entry in a list, and a ``service`` parameter (a ``pg_service.conf`` entry
    can name any host) are refused, because libpq would choose the host itself.
    """
    parsed = make_url(url)
    if any(key.lower() == "service" for key in parsed.query):
        pytest.fail(
            "OWNVITALS_TEST_DATABASE_URL must point at a loopback host by name, not through a "
            "service= entry: the network guard cannot see libpq connections"
        )
    given: list[str] = [parsed.host] if parsed.host else []
    for key in ("host", "hostaddr"):
        value = parsed.query.get(key, ())
        given.extend([value] if isinstance(value, str) else value)
    hosts = [host.strip() for entry in given for host in entry.split(",")]
    if not hosts or "" in hosts:
        pytest.fail(
            "OWNVITALS_TEST_DATABASE_URL must name a loopback host: with an empty one libpq "
            "picks the host itself, and the network guard cannot see libpq connections"
        )
    for host in hosts:
        if not _is_local_host(host):
            pytest.fail(
                f"OWNVITALS_TEST_DATABASE_URL must point at a loopback host, not {host!r}: "
                "the network guard cannot see libpq connections"
            )


def forget_libpq_host_environment() -> None:
    """Drop ``PGHOST``, ``PGHOSTADDR`` and ``PGSERVICE`` for the rest of the test process."""
    for name in LIBPQ_HOST_ENVIRONMENT:
        os.environ.pop(name, None)


def url_for(admin_url: str, role: str, password: str) -> str:
    """``admin_url`` with another role and password (same host, port and database)."""
    return (
        make_url(admin_url)
        .set(username=role, password=password)
        .render_as_string(hide_password=False)
    )


def alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@contextmanager
def migrated(owner_url: str, revision: str = "head") -> Iterator[None]:
    """The schema at ``revision`` (as the owner role, like ``alembic upgrade``); removed
    afterwards. ``env.py`` takes the owner role's URL from ``Settings``, so the URL is passed
    through the environment for the duration."""
    patch = pytest.MonkeyPatch()
    patch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", owner_url)
    try:
        forget_check_rows(owner_url)
        command.downgrade(alembic_config(), "base")
        # Alembic keeps its own table at base; a fresh one gets the owner's default privileges
        # again, so what the migrations revoke on it is checked every time, not inherited.
        drop_tables(owner_url, "alembic_version")
        command.upgrade(alembic_config(), revision)
        yield
    finally:
        forget_check_rows(owner_url)
        command.downgrade(alembic_config(), "base")
        patch.undo()


def forget_check_rows(owner_url: str) -> None:
    """Empty the key ring's check table, when it exists, as its owner. Migration 0004 refuses
    its downgrade while the table holds rows (a retired key's row could never be recreated);
    in the tests, every web or worker a test starts records them."""
    with owner_connection(owner_url) as connection:
        exists = connection.execute(text("SELECT to_regclass('public.connection_keks')")).scalar()
        if exists is not None:
            connection.execute(text("DELETE FROM public.connection_keks"))


def public_tables(database_url: str) -> set[str]:
    """The names of the tables in schema ``public``."""
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            return {row[0] for row in rows}
    finally:
        engine.dispose()


def applied_revisions(database_url: str) -> list[str]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).all()
    finally:
        engine.dispose()
    return [row[0] for row in rows]


# --- test-only owned tables (ADR 0002 section 7, guards 3 and 4) -------------------------------

PARENT, CHILD = "t_parent", "t_child"
OWNED_TABLES_DDL = [
    f"""CREATE TABLE {PARENT} (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL,
        name text NOT NULL,
        UNIQUE (owner_id, id),
        UNIQUE (owner_id, name))""",
    f"""CREATE TABLE {CHILD} (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL,
        parent_id uuid NOT NULL,
        UNIQUE (owner_id, id),
        FOREIGN KEY (owner_id, parent_id) REFERENCES {PARENT} (owner_id, id) ON DELETE CASCADE)""",
]


def run_rls_helper(connection: Connection, table: str) -> None:
    """Call the migration helper ``enable_owner_rls`` as an Alembic migration would."""
    with Operations.context(MigrationContext.configure(connection)):
        enable_owner_rls(table)


@contextmanager
def owner_connection(owner_url: str) -> Iterator[Connection]:
    """A transactional connection as ``ownvitals_owner`` (commits on success)."""
    engine = create_engine(owner_url)
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()


def drop_tables(owner_url: str, *tables: str) -> None:
    with owner_connection(owner_url) as connection:
        for table in tables:
            connection.execute(text(f'DROP TABLE IF EXISTS public."{table}" CASCADE'))


def create_owned_tables(owner_url: str) -> None:
    """``t_parent`` and ``t_child``, created by the owner and given the policy by the helper."""
    drop_tables(owner_url, CHILD, PARENT)
    with owner_connection(owner_url) as connection:
        for statement in OWNED_TABLES_DDL:
            connection.execute(text(statement))
        run_rls_helper(connection, PARENT)
        run_rls_helper(connection, CHILD)
