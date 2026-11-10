"""#303: the worker's metrics refresh reads nothing before the queue check has passed (ADR 0005
section 3). A drifted queue stops the worker before the refresh starts; a database that is down
at startup leaves the engine unverified, and the refresh task skips its ticks until it passes.
No account, no row: the refresh is replaced by a recorder.
"""

import asyncio
import urllib.request

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.config import Settings
from ownvitals.db import create_session_factory
from ownvitals.db.queue_check import QueueDriftError, queue_verified, verify_queue
from ownvitals.egress import core
from ownvitals.worker import metrics as worker_metrics
from ownvitals.worker.__main__ import main
from ownvitals.worker.loop import create_worker_engine
from ownvitals.worker.metrics import Refresher, metrics_running

REVOKE = text("REVOKE EXECUTE ON FUNCTION public.queue_reap(integer) FROM ownvitals_app")
GRANT = text("GRANT EXECUTE ON FUNCTION public.queue_reap(integer) TO ownvitals_app")


@pytest.fixture(autouse=True)
def _restore_worker_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` installs the process allowlist; put the process back afterwards."""
    monkeypatch.setattr(core, "_worker_list", None)
    monkeypatch.setattr(urllib.request, "_opener", None, raising=False)


@pytest.fixture
def refreshes(monkeypatch: pytest.MonkeyPatch) -> list[Refresher]:
    """Every call of ``Refresher.refresh``, which then reads nothing."""
    calls: list[Refresher] = []

    async def record(self: Refresher) -> None:
        calls.append(self)

    monkeypatch.setattr(Refresher, "refresh", record)
    return calls


async def _metrics_for(settings: Settings, *, verify: bool) -> None:
    """``metrics_running`` on the worker's engine, a few event-loop turns long."""
    engine = create_worker_engine(settings)
    try:
        if verify:
            await verify_queue(engine)
        stop = asyncio.Event()
        async with metrics_running(settings, create_session_factory(engine), stop, engine=engine):
            await asyncio.sleep(0.2)  # the first tick runs at once; the next is 5 s away
            stop.set()
    finally:
        await engine.dispose()


async def test_no_refresh_while_the_database_is_down_at_startup(
    unreachable_database_url: str, refreshes: list[Refresher]
) -> None:
    settings = Settings(database_url=unreachable_database_url)  # pyright: ignore[reportArgumentType]
    await _metrics_for(settings, verify=True)  # unreachable: returns, the engine stays unchecked
    assert refreshes == []


@pytest.mark.usefixtures("queue_schema")
async def test_no_refresh_when_verify_queue_fails(
    admin: Connection, app_database_url: str, refreshes: list[Refresher]
) -> None:
    admin.execute(REVOKE)
    try:
        settings = Settings(database_url=app_database_url, environment="test")  # pyright: ignore[reportArgumentType]
        with pytest.raises(QueueDriftError):
            await _metrics_for(settings, verify=True)
        # The same engine, past the refusal: still unchecked, so its refresh reads nothing.
        engine = create_worker_engine(settings)
        try:
            with pytest.raises(QueueDriftError):
                await verify_queue(engine)
            assert not queue_verified(engine)
            stop = asyncio.Event()
            factory = create_session_factory(engine)
            async with metrics_running(settings, factory, stop, engine=engine):
                await asyncio.sleep(0.2)
                stop.set()
        finally:
            await engine.dispose()
    finally:
        admin.execute(GRANT)
    assert refreshes == []


@pytest.mark.usefixtures("queue_schema", "restore_logging")
def test_main_on_a_drifted_queue_never_refreshes(
    monkeypatch: pytest.MonkeyPatch,
    admin: Connection,
    app_database_url: str,
    refreshes: list[Refresher],
) -> None:
    admin.execute(REVOKE)
    try:
        monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
        assert main() == 2
    finally:
        admin.execute(GRANT)
    assert refreshes == []


@pytest.mark.usefixtures("queue_schema")
async def test_the_refresh_runs_once_the_queue_check_passed(
    app_database_url: str, refreshes: list[Refresher]
) -> None:
    settings = Settings(database_url=app_database_url, environment="test")  # pyright: ignore[reportArgumentType]
    await _metrics_for(settings, verify=True)
    assert len(refreshes) == 1


async def test_a_tick_before_the_check_is_skipped_and_a_later_one_refreshes(
    monkeypatch: pytest.MonkeyPatch, unreachable_database_url: str, refreshes: list[Refresher]
) -> None:
    """The task keeps ticking while unchecked and refreshes from the first tick after the pass."""
    passed = asyncio.Event()
    monkeypatch.setattr(worker_metrics, "queue_verified", lambda _engine: passed.is_set())
    engine = create_worker_engine(Settings(database_url=unreachable_database_url))  # pyright: ignore[reportArgumentType]
    try:
        refresher = Refresher(create_session_factory(engine))
        stop = asyncio.Event()
        task = asyncio.ensure_future(
            worker_metrics._refresh_until(refresher, engine, 0.01, stop)  # pyright: ignore[reportPrivateUsage]
        )
        await asyncio.sleep(0.1)
        assert refreshes == []
        passed.set()
        for _ in range(100):
            if refreshes:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        await engine.dispose()
    assert refreshes
    assert all(r is refresher for r in refreshes)
