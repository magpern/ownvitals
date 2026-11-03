"""End to end (#164): a webhook notification queues ``withings_sync``, the worker process's
registry runs it against the fake, and the measures land in the owner's rows.

The worker here is the real loop with ``worker_registry()``, the auth account directory and the
webhook-target check, as ``python -m ownvitals.worker`` wires them (the schedule is left out: it
is ``test_queue_schedule.py``'s); only the network is the
in-process fake (``WithingsClient.open`` gets the fake's transport, every egress check still
runs). Synthetic user, tokens, webhook secret and measurements only.
"""

import asyncio
import contextlib
import urllib.request
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from jobapi import until
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.config import Settings
from ownvitals.connections.providers.withings import store_grant
from ownvitals.db import user_session
from ownvitals.egress import core, install_worker_allowlist
from ownvitals.ingest.cursors import write_cursor
from ownvitals.withings.apps import create_app
from ownvitals.withings.client import WithingsClient
from ownvitals.withings.subscriptions import derive_secret, pepper_hmac
from ownvitals.withings.webhooks import PREFIX, Cache, CachedEntry, Metrics, WebhookMiddleware
from ownvitals.worker.kinds import WORKER_TARGETS, worker_registry
from ownvitals.worker.loop import Worker, create_worker_engine
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")

CLIENT = "synthetic-client"
SECRET = "synthetic-secret"
BASE = "https://app.example.test"
REDIRECT = BASE + "/api/oauth/withings/callback"
# Labelled filler, not key material from a file.
SEED_BYTES = b"\x11" * 32
PEPPER_BYTES = b"\x22" * 32
MEDIA = "application/x-www-form-urlencoded"


@pytest.fixture(autouse=True)
def _worker_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


@pytest.fixture
async def owner_engine(owner_database_url: str) -> AsyncIterator[Any]:
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _through_fake(monkeypatch: pytest.MonkeyPatch, fake: FakeWithings) -> None:
    """Every client the handlers open answers from ``fake``; the egress client still runs."""
    original = WithingsClient.open

    def opened(app: Any, **options: Any) -> Any:
        return original(app, inner=FakeWithingsTransport(fake), **options)

    monkeypatch.setattr(WithingsClient, "open", opened)


def _receiver(env: Env, app_id: int, now: datetime) -> tuple[FastAPI, str]:
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


def _scalar(url: str, sql: str, **params: object) -> Any:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


@contextlib.asynccontextmanager
async def _worker(app_database_url: str) -> AsyncIterator[Worker]:
    settings = Settings(
        database_url=app_database_url,  # pyright: ignore[reportArgumentType]
        environment="test",
        worker_poll_seconds=0.1,
        job_lease_seconds=60,
    )
    engine = create_worker_engine(settings)
    worker = Worker(
        settings,
        engine,
        registry=worker_registry(),
        accounts=ACCOUNTS,
        targets=WORKER_TARGETS,
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop))
    try:
        yield worker
    finally:
        stop.set()
        await asyncio.wait_for(task, 40)
        await engine.dispose()


async def test_a_webhook_leads_to_a_sync_that_ingests_the_measures(
    env: Env,
    owner_engine: Any,
    app_database_url: str,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ring = env.app.state.keyring
    app_id = await create_app(owner_engine, env.factory, ring, CLIENT, BASE, SECRET)
    fake = FakeWithings(seed=164)
    fake.add_app(CLIENT, SECRET, (REDIRECT,))
    user = fake.add_user(days=30, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    tokens = fake.tokens_for(CLIENT, user.userid, scope="user.metrics")
    ctx = make_context(await env.user("e2e-sync@example.com"))
    stored = await store_grant(
        env.factory, ring, ctx, app_id, user.userid, "user.metrics",
        tokens["access_token"], tokens["refresh_token"], expires_in=10_800,
    )  # fmt: skip
    # The measures stream finished its backfill earlier: the sync reads changes since then.
    async with user_session(env.factory, ctx) as db:
        await write_cursor(
            db, ctx.user_id, stored.id, "measures", capability="supported",
            state={"status": "complete", "index": 1, "skipped_out_of_range": 0},
            lastupdate=1, zone="UTC",
        )  # fmt: skip
    expected = {
        group["grpid"] for group in user.data.measuregrps if group.get("category") in (1, 2)
    }
    assert expected

    now = datetime.now(UTC)
    web, presented = _receiver(env, app_id, now)
    body = (
        f"userid={user.userid}&appli=1&startdate={int(now.timestamp()) - 600}"
        f"&enddate={int(now.timestamp()) - 60}"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web), base_url="http://test"
    ) as client:
        answer = await client.post(
            PREFIX + presented, content=body.encode(), headers={"content-type": MEDIA}
        )
    assert answer.status_code == 200
    job_id = _scalar(database_url, "SELECT job_id FROM webhook_events")
    row = _scalar(database_url, "SELECT kind || ':' || origin FROM jobs WHERE id = :id", id=job_id)
    assert row == "withings_sync:webhook"

    install_worker_allowlist(ai_enabled=False)
    _through_fake(monkeypatch, fake)

    def state() -> str:
        return str(_scalar(database_url, "SELECT state FROM jobs WHERE id = :id", id=job_id))

    async with _worker(app_database_url):
        await until(lambda: state() in ("completed", "failed", "dead_letter"), seconds=30)
    assert state() == "completed"
    async with user_session(env.factory, ctx) as db:
        stored_ids = {
            int(grpid)
            for grpid in (
                await db.execute(
                    text("SELECT grpid FROM measure_groups WHERE owner_id = :o"), {"o": ctx.user_id}
                )
            ).scalars()
        }
    assert stored_ids == expected
    calls = [entry.endpoint for entry in fake.log]
    assert "measure:getmeas" in calls
