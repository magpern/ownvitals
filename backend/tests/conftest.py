"""Shared fixtures.

Database tests need a real Postgres: set ``OWNVITALS_TEST_DATABASE_URL`` (see
``docs/dev/backend.md``). Locally they skip with a reason when it is unset; in CI (``CI=true``) a
missing URL fails the run, so the database tests can never be skipped silently.
"""

import asyncio
import logging
import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import netguard
from dbtools import require_loopback_database, unavailable

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


@pytest.fixture
def database_url() -> str:
    """URL of the throwaway test Postgres (``postgresql+psycopg://...``), a superuser's.

    For test setup and probes, and until the database roles exist also for the code under test.
    It must be a loopback host: libpq connects natively, outside the network guard.
    """
    url = os.environ.get("OWNVITALS_TEST_DATABASE_URL")
    if not url:
        unavailable(
            "OWNVITALS_TEST_DATABASE_URL is not set (start a throwaway Postgres, "
            "see docs/dev/backend.md)"
        )
    require_loopback_database(url)
    return url


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
