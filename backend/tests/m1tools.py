"""Shared harness of the M1 done-when tests (#30): the real worker loop, a webhook receiver and
the fake Withings, in-process. Synthetic users, tokens, secrets and measurements only; no network.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text

from authtools import Env
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.config import Settings
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.subscriptions import derive_secret, pepper_hmac
from ownvitals.withings.webhooks import Cache, CachedEntry, Metrics, WebhookMiddleware
from ownvitals.worker.kinds import WORKER_TARGETS, worker_registry
from ownvitals.worker.loop import Worker, create_worker_engine
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport

CLIENT = "synthetic-client"
SECRET = "synthetic-secret"
BASE = "https://app.example.test"
REDIRECT = BASE + "/api/oauth/withings/callback"
# Labelled filler, not key material from a file.
SEED_BYTES = b"\x11" * 32
PEPPER_BYTES = b"\x22" * 32
MEDIA = "application/x-www-form-urlencoded"


def through_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeWithings) -> None:
    """Every client the handlers open answers from ``fake``; the egress client still runs."""
    original = WithingsClient.open

    def opened(app: Any, **options: Any) -> Any:
        return original(app, inner=FakeWithingsTransport(fake), **options)

    monkeypatch.setattr(WithingsClient, "open", opened)


def receiver(env: Env, app_id: int, now: datetime) -> tuple[FastAPI, str]:
    """The webhook middleware over the test database, and the one secret it accepts."""
    presented = derive_secret(SEED_BYTES, app_id, uuid4())
    entry = CachedEntry(
        app_id, str(uuid4()), pepper_hmac(PEPPER_BYTES, presented), PEPPER_BYTES,
        now - timedelta(days=1), None,
    )  # fmt: skip
    web = FastAPI()
    web.state.session_factory = env.factory
    web.state.webhook_cache = Cache(entries=[entry], read_at=now)
    web.state.webhook_metrics = Metrics()
    web.state.webhook_clock = lambda: now
    web.add_middleware(WebhookMiddleware)
    return web, presented


def scalar(url: str, sql: str, **params: object) -> Any:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


def refill(url: str) -> None:
    """Test-only: hand the app's rate-limit bucket its full budget back and make delayed retries
    due, so a backfill that the real limiter (100 calls a minute) would spread over minutes ends
    in seconds. The limiter itself is covered by test_withings_rate.py."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE withings_rate_buckets SET remaining = budget, paused_until = NULL")
            )
            conn.execute(
                text(
                    "UPDATE jobs SET available_at = now() "
                    "WHERE state = 'queued' AND available_at > now()"
                )
            )
    finally:
        engine.dispose()


@contextlib.asynccontextmanager
async def running_worker(
    app_database_url: str, *, lease_seconds: int = 60
) -> AsyncIterator[Worker]:
    """The real worker loop with the real registry, as ``python -m ownvitals.worker`` wires it."""
    settings = Settings(
        database_url=app_database_url,  # pyright: ignore[reportArgumentType]
        environment="test",
        worker_poll_seconds=0.1,
        job_lease_seconds=lease_seconds,
    )
    engine = create_worker_engine(settings)
    worker = Worker(
        settings, engine, registry=worker_registry(), accounts=ACCOUNTS, targets=WORKER_TARGETS
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop))
    try:
        yield worker
    finally:
        stop.set()
        await asyncio.wait_for(task, 40)
        await engine.dispose()
