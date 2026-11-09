"""``python -m ownvitals.worker`` (ADR 0005 section 5): start, stop, a database that is down, and
the refusals (exit code 2) on a privileged role, a drifted queue or a refused key ring."""

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobtools import admin_fixture, install_queue, queue_schema_fixture  # noqa: F401 - fixtures
from keystools import KEY_D, app_factory, write_key
from ownvitals.config import Settings
from ownvitals.crypto import keks as key_module
from ownvitals.crypto.envelope import RingKey
from ownvitals.crypto.keks import load_keyring
from ownvitals.egress import core
from ownvitals.worker import loop as worker_loop
from ownvitals.worker.__main__ import main
from ownvitals.worker.loop import (
    Worker,
    create_worker_engine,
    run_worker,
    worker_id,
    worker_pool,
)


@pytest.fixture(autouse=True)
def _restore_worker_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` installs the process allowlist; put the process back afterwards."""
    monkeypatch.setattr(core, "_worker_list", None)
    monkeypatch.setattr(urllib.request, "_opener", None, raising=False)


async def _must_not_claim(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the worker claimed before startup finished")


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


@pytest.mark.usefixtures("restore_logging")
def test_main_refuses_to_start_without_the_allowlist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("ownvitals.worker.__main__.install_worker_allowlist", lambda **_k: None)
    monkeypatch.setattr("ownvitals.worker.__main__.run_worker", _must_not_claim)
    assert main() == 2
    assert "allowlist is not installed" in capsys.readouterr().out


@pytest.mark.usefixtures("restore_logging", "queue_schema")
def test_main_refuses_a_missing_kek(
    monkeypatch: pytest.MonkeyPatch,
    app_database_url: str,
    secrets_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (secrets_dir / "connection_kek").unlink()
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
    monkeypatch.setattr("ownvitals.worker.__main__.run_worker", _must_not_claim)
    assert main() == 2
    assert "secret file is missing" in capsys.readouterr().out


@pytest.mark.usefixtures("restore_logging", "queue_schema")
@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "metrics.example.test", "8.8.8.8"])  # noqa: S104
def test_main_refuses_a_metrics_bind_that_is_not_internal(
    monkeypatch: pytest.MonkeyPatch,
    app_database_url: str,
    secrets_dir: Path,
    capsys: pytest.CaptureFixture[str],
    bind: str,
) -> None:
    """#167: the /metrics listener binds a loopback or private literal only; else exit 2."""
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
    monkeypatch.setenv("OWNVITALS_METRICS_BIND", bind)
    monkeypatch.setattr("ownvitals.worker.__main__.run_worker", _must_not_claim)
    assert main() == 2
    assert "metrics bind" in capsys.readouterr().out


@pytest.mark.usefixtures("restore_logging", "queue_schema")
def test_main_refuses_a_malformed_kek(
    monkeypatch: pytest.MonkeyPatch,
    app_database_url: str,
    secrets_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (secrets_dir / "connection_kek").write_bytes(b"not-a-key\n")
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
    monkeypatch.setattr("ownvitals.worker.__main__.run_worker", _must_not_claim)
    assert main() == 2
    assert "64 lowercase" in capsys.readouterr().out


@pytest.mark.usefixtures("restore_logging", "queue_schema")
def test_main_refuses_a_swapped_kek(
    monkeypatch: pytest.MonkeyPatch,
    app_database_url: str,
    secrets_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(database_url=app_database_url, environment="test")  # pyright: ignore[reportArgumentType]

    async def record() -> None:
        async with app_factory(settings) as factory:
            assert (await load_keyring(factory, settings, role="worker")).verified

    asyncio.run(record())
    write_key(secrets_dir, "connection_kek", KEY_D)  # synthetic: another key, same id
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
    monkeypatch.setattr("ownvitals.worker.__main__.run_worker", _must_not_claim)
    assert main() == 2
    assert "does not match the check value" in capsys.readouterr().out


def test_the_worker_id_fits_the_claim_function() -> None:
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", worker_id())
    assert worker_id() != worker_id()


def _watch_claim_and_reap(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record ``queue.claim`` and ``queue.reap``. A call fails the test that holds the list."""
    invoked: list[str] = []

    async def claim(*_args: object, **_kwargs: object) -> None:
        invoked.append("claim")
        return None

    async def reap(*_args: object, **_kwargs: object) -> int:
        invoked.append("reap")
        return 0

    monkeypatch.setattr(worker_loop.queue, "claim", claim)
    monkeypatch.setattr(worker_loop.queue, "reap", reap)
    return invoked


def _stop_after(monkeypatch: pytest.MonkeyPatch, passes: int, seen: dict[str, int]) -> None:
    """Bound ``Worker.run`` so a loop that never stops still ends the test."""
    real_run = Worker.run

    async def bounded(self: Worker, stop: asyncio.Event) -> None:
        async def cap() -> None:
            await asyncio.sleep(0.05 * passes)
            stop.set()

        limiter = asyncio.create_task(cap())
        try:
            await real_run(self, stop)
        finally:
            seen["passes"] = self.passes
            limiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await limiter

    monkeypatch.setattr(Worker, "run", bounded)


@pytest.mark.usefixtures("restore_logging")
def test_an_unverified_key_ring_is_not_claimed(
    monkeypatch: pytest.MonkeyPatch,
    unreachable_database_url: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The queue gate can be open while the ring is still unverified: claim and reap wait,
    and a database that stays unreachable is not exit 2."""
    invoked = _watch_claim_and_reap(monkeypatch)
    seen: dict[str, int] = {"passes": 0}
    _stop_after(monkeypatch, 6, seen)
    monkeypatch.setattr(worker_loop, "queue_verified", lambda _engine: True)
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", unreachable_database_url)
    monkeypatch.setenv("OWNVITALS_WORKER_POLL_SECONDS", "0.05")
    assert main() == 0
    assert invoked == []
    assert seen["passes"] >= 2
    output = capsys.readouterr().out
    assert "worker started" in output
    assert "worker refused to run" not in output


@pytest.mark.usefixtures("restore_logging", "queue_schema")
def test_a_deferred_key_mismatch_stops_before_claim(
    monkeypatch: pytest.MonkeyPatch,
    app_database_url: str,
    secrets_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The startup check was deferred. When it then runs and the key does not match, the
    worker exits 2 and has not claimed or reaped. Synthetic key material only."""
    settings = Settings(database_url=app_database_url, environment="test")  # pyright: ignore[reportArgumentType]

    async def record() -> None:
        async with app_factory(settings) as factory:
            assert (await load_keyring(factory, settings, role="worker")).verified

    asyncio.run(record())
    write_key(secrets_dir, "connection_kek", KEY_D)  # synthetic: another key, same id
    real_check = key_module._check_rows  # pyright: ignore[reportPrivateUsage]
    calls = {"n": 0}

    async def defer_once(factory: async_sessionmaker[AsyncSession], keys: list[RingKey]) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # the database was unreachable at the startup check
        return await real_check(factory, keys)

    monkeypatch.setattr(key_module, "_check_rows", defer_once)  # pyright: ignore[reportPrivateUsage]
    invoked = _watch_claim_and_reap(monkeypatch)
    seen: dict[str, int] = {"passes": 0}
    _stop_after(monkeypatch, 40, seen)
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
    monkeypatch.setenv("OWNVITALS_WORKER_POLL_SECONDS", "0.05")
    assert main() == 2
    assert calls["n"] >= 2  # the deferred startup check, then the mismatch
    assert invoked == []
    output = capsys.readouterr().out
    assert "worker started" in output
    assert "does not match the check value" in output
