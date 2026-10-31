"""``python -m ownvitals.worker`` (ADR 0005 section 5): start, stop, a database that is down, and
the refusals (exit code 2) on a privileged role or a drifted queue."""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection

from jobtools import admin_fixture, install_queue, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.config import Settings
from ownvitals.worker.__main__ import main
from ownvitals.worker.loop import (
    Worker,
    create_worker_engine,
    run_worker,
    worker_id,
    worker_pool,
)


def _down(url: str, poll: float = 0.02) -> Settings:
    # A closed port: the loop keeps polling (no claim before the queue check passed).
    return Settings(database_url=url, worker_poll_seconds=poll)  # pyright: ignore[reportArgumentType]


async def test_the_worker_logs_start_and_stop_and_exits_when_the_stop_event_is_set(
    caplog: pytest.LogCaptureFixture, unreachable_database_url: str
) -> None:
    stop = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="ownvitals.worker.loop"):
        task = asyncio.create_task(run_worker(_down(unreachable_database_url), stop))
        await asyncio.sleep(0.3)
        assert not task.done()  # a database that is down does not stop the worker
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    messages = [r.getMessage() for r in caplog.records if r.name == "ownvitals.worker.loop"]
    assert messages == ["worker started", "worker stopped"]


async def test_the_worker_with_the_stop_event_already_set_returns_at_once(
    unreachable_database_url: str,
) -> None:
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(run_worker(_down(unreachable_database_url), stop), timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals; the container runs Linux")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_the_worker_process_exits_0_on_a_signal(
    sig: signal.Signals, unreachable_database_url: str
) -> None:
    # A closed port: a database that answers with a refused login would stop the worker.
    env = {**os.environ, "OWNVITALS_DATABASE_URL": unreachable_database_url}
    command = [sys.executable, "-m", "ownvitals.worker"]
    with subprocess.Popen(  # noqa: S603 - our own interpreter and module
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
    ) as process:
        try:
            assert process.stdout is not None
            first = json.loads(process.stdout.readline())  # blocks until the worker is up
            assert first["message"] == "worker started"
            process.send_signal(sig)
            assert process.wait(timeout=5) == 0
            rest = [json.loads(line) for line in process.stdout.read().splitlines()]
            assert rest[-1]["message"] == "worker stopped"
            assert all(entry["level"] != "ERROR" for entry in rest)
        finally:
            process.kill()


async def test_with_the_database_down_the_loop_waits_its_poll_and_backs_off_its_checks(
    unreachable_database_url: str,
) -> None:
    settings = _down(unreachable_database_url, poll=0.1)
    engine = create_worker_engine(settings)
    worker = Worker(settings, engine)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.sleep(1.5)
        assert not task.done()
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
        await engine.dispose()
    # A poll of 0.1 s gives about 15 passes: no hot loop while the database is down.
    assert worker.passes <= 30
    # One check at a time, with backoff (0.1, 0.2, 0.4, 0.8 s ...): only a handful in 1.5 s.
    assert 1 <= worker.checks <= 6


def test_the_worker_pool_fits_its_concurrency() -> None:
    for n in (1, 2, 16):
        pool = worker_pool(Settings(worker_concurrency=n))
        # a handler and a heartbeat per run, the listener and the claim/maintenance connection
        assert pool["pool_size"] == 2 * n + 2
        assert pool["pool_size"] + pool["max_overflow"] <= 50
    with pytest.raises(ValidationError):
        Settings(worker_concurrency=17)


@pytest.mark.usefixtures("restore_logging")
def test_main_refuses_a_privileged_role_with_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, queue_schema: str
) -> None:
    """The test superuser is privileged (ADR 0002 section 5): no override, exit code 2."""
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", queue_schema)
    assert main() == 2


@pytest.mark.usefixtures("restore_logging")
def test_main_refuses_a_drifted_queue_with_exit_code_2(
    monkeypatch: pytest.MonkeyPatch, admin: Connection, app_database_url: str
) -> None:
    admin.execute(text("REVOKE EXECUTE ON FUNCTION public.queue_reap(integer) FROM ownvitals_app"))
    try:
        monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
        assert main() == 2
    finally:
        install_queue(admin.engine.url.render_as_string(hide_password=False))


def test_the_worker_id_fits_the_claim_function() -> None:
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", worker_id())
    assert worker_id() != worker_id()
