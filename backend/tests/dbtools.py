"""Helpers for the tests that need the throwaway Postgres (``OWNVITALS_TEST_DATABASE_URL``)."""

import ipaddress
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

BACKEND_DIR = Path(__file__).resolve().parent.parent


def unavailable(reason: str) -> NoReturn:
    """The test database is missing: fail in CI (``CI=true``), skip anywhere else, so the
    database tests can never be skipped silently where they must run."""
    if os.environ.get("CI") == "true":
        pytest.fail(reason + "; database tests must run in CI")
    pytest.skip(reason)


def _is_local_host(host: str) -> bool:
    """Loopback by name or address, or a Unix socket directory; an empty host is the default
    socket of the machine."""
    if host in ("", "localhost") or host.startswith("/"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def require_loopback_database(url: str) -> None:
    """Fail unless every host ``url`` can connect to is loopback or a local socket.

    The network guard patches Python's sockets, but libpq opens its own: a test database URL
    pointing elsewhere would connect (and send test data) unseen. ``host`` and ``hostaddr``
    may also come in the query string, and a host can be a comma-separated list.
    """
    parsed = make_url(url)
    given: list[str] = [parsed.host or ""]
    for key in ("host", "hostaddr"):
        value = parsed.query.get(key, ())
        given.extend([value] if isinstance(value, str) else value)
    for entry in given:
        for host in entry.split(","):
            if not _is_local_host(host.strip()):
                pytest.fail(
                    f"OWNVITALS_TEST_DATABASE_URL must point at a loopback host, not {host!r}: "
                    "the network guard cannot see libpq connections"
                )


def alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@contextmanager
def migrated(database_url: str, revision: str = "head") -> Iterator[None]:
    """The schema at ``revision`` (as ``alembic upgrade`` would make it); removed afterwards.

    ``env.py`` takes the owner role's URL from ``Settings``, so the URL is passed through the
    environment for the duration. Until the roles exist the owner is the test superuser.
    """
    patch = pytest.MonkeyPatch()
    patch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", database_url)
    try:
        command.downgrade(alembic_config(), "base")
        command.upgrade(alembic_config(), revision)
        yield
    finally:
        command.downgrade(alembic_config(), "base")
        patch.undo()


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
