"""Web keeps its copy of the webhook-secret HMACs fresh after startup (#322, ADR 0008).

Before #322 web read the copy once in its lifespan; once the 60 s TTL passed every webhook POST
answered 503. No real account, secret or measurement: the presented secret is a synthetic
string that matches no entry, so a fresh copy answers the generic 200 and a stale one 503.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from authtools import (  # noqa: F401 - fixtures
    ORIGIN,
    auth_schema_fixture,
    clean_auth_fixture,
    make_settings,
)
from jobapi import until
from ownvitals.config import Settings
from ownvitals.web import create_app
from ownvitals.withings import webhook_refresh
from ownvitals.withings.webhook_cache import CACHE_TTL, Cache
from ownvitals.withings.webhook_receiver import PREFIX

MEDIA = "application/x-www-form-urlencoded"
# Synthetic: 43 url-safe characters like a minted secret, matching no stored entry.
UNKNOWN_SECRET = "synthetic-unknown-secret-0000000000000000000"
LEAK = "synthetic-detail-that-must-not-be-logged"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _form(now: datetime) -> bytes:
    start = int(now.timestamp()) - 600
    return f"userid=424242&appli=1&startdate={start}&enddate={start + 60}".encode()


async def _post(app: FastAPI, now: datetime) -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        response = await client.post(
            PREFIX + UNKNOWN_SECRET, content=_form(now), headers={"content-type": MEDIA}
        )
    return response.status_code


# --- the setting ------------------------------------------------------------------------------


def test_the_cache_ttl_defaults_to_the_append_lead() -> None:
    assert Settings().webhook_cache_ttl_seconds == CACHE_TTL.total_seconds() == 60


@pytest.mark.parametrize("value", [0, 3, 61, 3600])
def test_the_cache_ttl_can_be_shortened_but_never_past_the_append_lead(value: int) -> None:
    # Longer than append_secret's 60 s lead, an entry could open before a web process holds it.
    with pytest.raises(ValidationError):
        Settings(webhook_cache_ttl_seconds=value)


def test_the_cache_ttl_reads_its_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNVITALS_WEBHOOK_CACHE_TTL_SECONDS", "20")
    assert Settings().webhook_cache_ttl_seconds == 20


# --- the refresher, without a database --------------------------------------------------------


def test_reads_run_four_times_per_ttl() -> None:
    assert webhook_refresh.interval_for(timedelta(seconds=60)) == 15
    assert webhook_refresh.interval_for(timedelta(seconds=4)) == 1


def _stub(monkeypatch: pytest.MonkeyPatch, load: Callable[[FastAPI], object]) -> None:
    monkeypatch.setattr(webhook_refresh, "load_cache", load)


def _app_with_copy(read_at: datetime) -> tuple[FastAPI, Cache]:
    app = FastAPI()
    cache = Cache(entries=[], read_at=read_at)
    app.state.webhook_cache = cache
    return app, cache


async def test_a_failed_read_keeps_the_copy_and_logs_the_error_class_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing(app: FastAPI) -> None:
        raise RuntimeError(LEAK)

    _stub(monkeypatch, failing)
    stamp = datetime(2026, 6, 1, tzinfo=UTC)
    app, cache = _app_with_copy(stamp)
    with caplog.at_level(logging.WARNING, logger=webhook_refresh.__name__):
        assert await webhook_refresh.refresh_once(app, 1.0) is False
    assert cache.read_at == stamp
    (record,) = caplog.records
    assert record.getMessage() == "webhook cache not refreshed"
    assert record.__dict__["error_type"] == "RuntimeError"
    assert LEAK not in caplog.text


async def test_a_read_that_hangs_is_abandoned_and_the_copy_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hanging(app: FastAPI) -> None:
        await asyncio.sleep(30)

    _stub(monkeypatch, hanging)
    stamp = datetime(2026, 6, 1, tzinfo=UTC)
    app, cache = _app_with_copy(stamp)
    assert await webhook_refresh.refresh_once(app, 0.05) is False
    assert cache.read_at == stamp


async def test_the_task_keeps_reading_after_a_failure_and_stops_on_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def flaky(app: FastAPI) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("synthetic")
        app.state.webhook_cache.read_at = datetime.now(UTC)

    _stub(monkeypatch, flaky)
    stamp = datetime(2026, 6, 1, tzinfo=UTC)
    app, cache = _app_with_copy(stamp)
    task = webhook_refresh.start(app, timedelta(seconds=0.2))
    try:
        assert cache.ttl == timedelta(seconds=0.2)
        assert calls == []  # the first read is one interval after start
        await until(lambda: len(calls) >= 3, seconds=5)
        assert cache.read_at != stamp  # a read after the failure replaced the copy
    finally:
        await webhook_refresh.stop(task)
    assert task.done()
    seen = len(calls)
    await asyncio.sleep(0.15)
    assert len(calls) == seen  # stopped: no further reads
    await webhook_refresh.stop(None)


async def test_start_gives_an_app_without_a_copy_an_empty_stale_one() -> None:
    app = FastAPI()
    task = webhook_refresh.start(app, timedelta(seconds=8))
    try:
        cache = app.state.webhook_cache
        assert isinstance(cache, Cache)
        assert cache.ttl == timedelta(seconds=8)
        assert not cache.fresh(datetime.now(UTC))  # fail closed until a read succeeds
    finally:
        await webhook_refresh.stop(task)


# --- the real lifespan, on the app role -------------------------------------------------------


@pytest.mark.usefixtures("clean_auth")
async def test_the_receiver_still_answers_after_the_ttl_has_passed(app_database_url: str) -> None:
    """Fails before #322: the lifespan read the copy once and never again, so past the TTL
    every POST answered 503."""
    clock = Clock()
    app = create_app(make_settings(app_database_url, webhook_cache_ttl_seconds=4))
    app.state.webhook_clock = clock
    async with app.router.lifespan_context(app):
        cache = app.state.webhook_cache
        assert isinstance(cache, Cache)
        assert cache.ttl == timedelta(seconds=4)
        assert cache.read_at == clock.now
        assert await _post(app, clock.now) == 200

        first = clock.now
        clock.now = first + timedelta(seconds=5)  # past the TTL
        assert not cache.fresh(clock.now)
        await until(lambda: cache.read_at == clock.now, seconds=5)
        assert await _post(app, clock.now) == 200

        clock.now += timedelta(seconds=5)  # again: every later read keeps it fresh
        await until(lambda: cache.read_at == clock.now, seconds=5)
        assert await _post(app, clock.now) == 200
    assert app.state.webhook_cache.read_at is not None


@pytest.mark.usefixtures("clean_auth")
async def test_a_stale_copy_still_answers_503(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed is kept: while every read fails, the copy goes stale and POST answers 503."""
    clock = Clock()
    app = create_app(make_settings(app_database_url, webhook_cache_ttl_seconds=4))
    app.state.webhook_clock = clock
    async with app.router.lifespan_context(app):
        assert await _post(app, clock.now) == 200

        failures: list[int] = []

        async def failing(app: FastAPI) -> None:
            failures.append(1)
            raise OSError("synthetic")

        _stub(monkeypatch, failing)
        clock.now += timedelta(seconds=5)
        assert await _post(app, clock.now) == 503
        await until(lambda: len(failures) >= 2, seconds=5)  # rounds ran and failed
        assert await _post(app, clock.now) == 503
