"""Shared fixtures.

Database tests need a real Postgres: set ``OWNVITALS_TEST_DATABASE_URL`` (see
``docs/dev/backend.md``) and create the roles in it (``docs/dev/isolation.md``). Locally they skip
with a reason when either is missing; in CI (``CI=true``) a missing URL or role fails the run, so
the database tests can never be skipped silently.
"""

import asyncio
import logging
import os
import secrets
import socket
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

import netguard
from dbtools import (
    CHILD,
    PARENT,
    create_owned_tables,
    drop_tables,
    forget_libpq_host_environment,
    require_loopback_database,
    unavailable,
    url_for,
)

if sys.platform == "win32":
    # psycopg's async mode does not run on the default Windows (proactor) event loop.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OWNVITALS_* setting leaks in from the machine running the tests."""
    for name in list(os.environ):
        if name.startswith("OWNVITALS_") and name != "OWNVITALS_TEST_DATABASE_URL":
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def secrets_dir(
    _isolated_environment: None,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """An empty secrets directory, so ``/run/secrets`` of the machine never leaks into a test.
    Tests that need secret files write them here."""
    directory = tmp_path_factory.mktemp("secrets")
    monkeypatch.setenv("OWNVITALS_SECRETS_DIR", str(directory))
    return directory


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "allow_network: may reach non-loopback hosts (unused)")


@pytest.fixture(autouse=True)
def _loopback_only(request: pytest.FixtureRequest) -> Iterator[None]:
    """No test sends a packet beyond loopback (``netguard``) unless it is marked allow_network.

    Its own ``MonkeyPatch``: a test calling ``monkeypatch.undo()`` cannot lift the guard."""
    if request.node.get_closest_marker("allow_network") is not None:
        yield
        return
    with pytest.MonkeyPatch.context() as guard:
        netguard.install(guard)
        yield


def _test_database_url() -> str:
    """``OWNVITALS_TEST_DATABASE_URL``, checked: a loopback host, and no ``PGHOST``,
    ``PGHOSTADDR`` or ``PGSERVICE`` left in the process for libpq to pick another one from. It
    must be a loopback host because libpq connects natively, outside the network guard."""
    forget_libpq_host_environment()
    url = os.environ.get("OWNVITALS_TEST_DATABASE_URL")
    if not url:
        unavailable(
            "OWNVITALS_TEST_DATABASE_URL is not set (start a throwaway Postgres, "
            "see docs/dev/backend.md)"
        )
    require_loopback_database(url)
    return url


@pytest.fixture
def database_url() -> str:
    """Superuser URL of the throwaway test Postgres (``postgresql+psycopg://...``).

    For test setup and probes only. Code under test connects as ``app_database_url`` (web and
    worker) or ``owner_database_url`` (migrations): web and worker refuse a superuser.
    """
    return _test_database_url()


APP_ROLE, OWNER_ROLE = "ownvitals_app", "ownvitals_owner"


@dataclass(frozen=True)
class RoleUrls:
    owner: str
    app: str


@pytest.fixture(scope="session")
def role_urls() -> RoleUrls:
    """URLs of ``ownvitals_owner`` and ``ownvitals_app``, whose roles
    ``docker/postgres/init/10-roles.sql`` created (``docs/dev/isolation.md``). Their passwords
    are replaced by random ones for this run."""
    admin_url = _test_database_url()
    passwords = {OWNER_ROLE: secrets.token_hex(16), APP_ROLE: secrets.token_hex(16)}
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            found = (
                connection.execute(
                    text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
                    {"names": list(passwords)},
                )
                .scalars()
                .all()
            )
            if sorted(found) != sorted(passwords):
                unavailable(
                    "the roles of docker/postgres/init/10-roles.sql do not exist in the test "
                    "database (run it first, see docs/dev/isolation.md)"
                )
            for role, password in passwords.items():  # both are hex: nothing to quote
                connection.execute(text(f"ALTER ROLE {role} PASSWORD '{password}'"))
    finally:
        engine.dispose()
    return RoleUrls(
        owner=url_for(admin_url, OWNER_ROLE, passwords[OWNER_ROLE]),
        app=url_for(admin_url, APP_ROLE, passwords[APP_ROLE]),
    )


@pytest.fixture
def app_database_url(role_urls: RoleUrls) -> str:
    """URL of the app role, as web and worker connect."""
    return role_urls.app


@pytest.fixture
def owner_database_url(role_urls: RoleUrls) -> str:
    """URL of the owner role, as migrations connect."""
    return role_urls.owner


@pytest.fixture
def owned_tables(owner_database_url: str) -> Iterator[None]:
    """Test-only owned parent and child tables (``dbtools.PARENT``, ``CHILD``), as a migration
    by the owner would make them, with the policy of ``enable_owner_rls``; dropped afterwards."""
    create_owned_tables(owner_database_url)
    try:
        yield
    finally:
        drop_tables(owner_database_url, CHILD, PARENT)


@pytest.fixture
def unreachable_database_url() -> str:
    """A URL whose port is closed on this machine, for the 'database is down' cases."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"postgresql+psycopg://ownvitals:not-a-real-password@127.0.0.1:{port}/ownvitals"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """For tests that call ``configure_logging``: put the root logger back afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
