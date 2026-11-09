"""M1 done-when, first item (PLAN section 4.4; issue #30): the whole flow against the fake.

Connect (OAuth state and code exchange), the connect job, backfill with progress events, a
webhook-driven sync, the poll, the reconciliation, then disconnect and delete. The worker is the
real loop with the real registry; only the network is the in-process fake. Synthetic data only.
"""

import json
import urllib.request
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from jobapi import until
from m1tools import (
    BASE,
    CLIENT,
    MEDIA,
    REDIRECT,
    SECRET,
    receiver,
    refill,
    running_worker,
    scalar,
    through_fake,
)  # fmt: skip
from ownvitals.connections.purge import purge_connection
from ownvitals.egress import core, install_worker_allowlist
from ownvitals.jobs.queue import enqueue
from ownvitals.withings import streams
from ownvitals.withings.apps import create_app
from ownvitals.withings.disconnect import disconnect_withings
from ownvitals.withings.oauth import SessionProof, StateStore, callback, start
from ownvitals.withings.webhooks import PREFIX
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingswire import Request

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")


@pytest.fixture(autouse=True)
def _web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


@pytest.fixture
async def owner_engine(owner_database_url: str) -> AsyncIterator[Any]:
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _queue(env: Env, connection: UUID, kind: str) -> None:
    await enqueue(
        env.factory,
        connection_id=connection,
        kind=kind,
        origin="scheduler",
        payload=json.dumps({"connection_id": str(connection)}),
        singleton_key=None,
        max_attempts=3,
        target_secret_id=None,
    )


_BROKEN = (
    "SELECT coalesce(string_agg(state || ':' || coalesce(error_category, '-'), ','), '') "
    "FROM jobs WHERE {where} AND "
    "state IN ('failed', 'dead_letter')"
)
_COMPLETED = "SELECT count(*) FROM jobs WHERE {where} AND state = 'completed'"


_SUMMARY = (
    "SELECT coalesce(string_agg(kind || '/' || origin || '/' || state || '/' || attempt::text "
    "|| '/' || coalesce(error_category, '-') || '/' || (available_at > now())::text "
    "|| '/' || coalesce((SELECT string_agg(e.payload->>'category', ',') FROM job_events e "
    "WHERE e.job_id = jobs.id AND e.payload ? 'category'), '-'), '; '), '') "
    "FROM jobs"
)


async def _settled(url: str, kind: str) -> None:
    """Until no job of ``kind`` is queued or running (a chain of per-stream jobs has ended)."""
    pending = "SELECT count(*) FROM jobs WHERE kind = :k AND state IN ('queued', 'running')"

    def check() -> bool:
        refill(url)
        return not scalar(url, pending, k=kind)

    await until(check, seconds=90)


async def _done(url: str, kind: str, stream: str | None = None) -> None:
    """Until a job of ``kind`` (and ``stream``) has completed. A failed or dead-lettered one of
    that kind fails at once with its error category, so a broken step never waits out a deadline.
    Jobs that are merely queued later (debounced syncs, derived-data kinds) are not waited for."""
    params = {"k": kind, "s": stream}
    match = "kind = :k AND (CAST(:s AS text) IS NULL OR input->>'stream' = :s)"

    def check() -> bool:
        refill(url)
        broken = scalar(url, _BROKEN.replace("{where}", match), **params)
        assert not broken, f"{kind} {stream or ''} did not complete: {broken}"
        return bool(scalar(url, _COMPLETED.replace("{where}", match), **params))

    try:
        await until(check, seconds=60)
    except AssertionError as error:
        if "did not complete" in str(error):
            raise
        raise AssertionError(
            f"timed out waiting for {kind}; jobs: {scalar(url, _SUMMARY)}"
        ) from None


def _states(url: str, kind: str) -> list[str]:
    engine_rows = scalar(
        url, "SELECT coalesce(string_agg(state, ','), '') FROM jobs WHERE kind = :k", k=kind
    )
    return [s for s in str(engine_rows).split(",") if s]


async def test_connect_backfill_webhook_poll_reconcile_disconnect_and_delete(
    env: Env,
    owner_engine: Any,
    app_database_url: str,
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ring = env.app.state.keyring
    app_id = await create_app(owner_engine, env.factory, ring, CLIENT, BASE, SECRET)
    fake = FakeWithings(seed=30, rate_limit=None)
    fake.add_app(CLIENT, SECRET, (REDIRECT,))
    user = fake.add_user(days=30, ecg_count=0, sleep_series_nights=0, intraday_days=0)
    expected = {g["grpid"] for g in user.data.measuregrps if g.get("category") in (1, 2)}
    assert expected
    ctx = make_context(await env.user("m1-flow@example.com"))

    # Connect: the real OAuth state and code exchange against the fake.
    store, proof, inner = StateStore(), SessionProof(uuid4(), True), FakeWithingsTransport(fake)
    started = await start(env.factory, ring, ctx, proof, app_id, store=store)
    page = fake.handle(Request("GET", started.authorize_url))
    code = parse_qs(urlsplit(page.headers["location"]).query)["code"][0]
    stored = await callback(
        env.factory, ring, ctx, proof, code, started.state, store=store, inner=inner
    )
    install_worker_allowlist(ai_enabled=False)
    through_fake(monkeypatch, fake)
    # The synthetic history is 30 days: walk 45, not 17 years of windows, so the backfill is a
    # handful of calls per stream.
    monkeypatch.setattr(streams, "FLOOR", datetime.now(UTC) - timedelta(days=45))

    def measure_count() -> int:
        return int(
            scalar(database_url, "SELECT count(*) FROM measure_groups WHERE owner_id = :o",
                   o=ctx.user_id)
        )  # fmt: skip

    async with running_worker(app_database_url):
        # Connect job, then the backfill jobs it queues, with progress events.
        await _queue(env, stored.id, "withings_connect")
        await _done(database_url, "withings_connect")
        await _done(database_url, "withings_backfill", "measures")
        await _settled(database_url, "withings_backfill")
        assert _states(database_url, "withings_connect") == ["completed"]
        assert _states(database_url, "withings_backfill").count("completed") >= 1
        assert measure_count() == len(expected)
        status = scalar(
            database_url,
            "SELECT backfill_state->>'status' FROM sync_cursors "
            "WHERE connection_id = :c AND stream = 'measures'",
            c=stored.id,
        )
        assert status == "complete"
        assert scalar(
            database_url,
            "SELECT count(*) FROM job_events e JOIN jobs j ON j.id = e.job_id "
            "WHERE j.kind = 'withings_backfill' AND e.kind = 'progress'",
        ) > 0  # fmt: skip

        # Webhook-driven sync: the notification queues one job, a duplicate collapses into it.
        now = datetime.now(UTC)
        web, presented = receiver(env, app_id, now)
        body = (
            f"userid={user.userid}&appli=1&startdate={int(now.timestamp()) - 600}"
            f"&enddate={int(now.timestamp()) - 60}"
        ).encode()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web), base_url="http://test"
        ) as client:
            for _ in range(2):
                answer = await client.post(
                    PREFIX + presented, content=body, headers={"content-type": MEDIA}
                )
                assert answer.status_code == 200
        assert scalar(database_url, "SELECT count(*) FROM webhook_events") == 1
        # A known user's notification must have queued a job (a null job_id is unknown_user).
        assert (
            scalar(database_url, "SELECT count(*) FROM webhook_events WHERE job_id IS NOT NULL")
            == 1
        )
        await _done(database_url, "withings_sync")
        assert not {"failed", "dead_letter"} & set(_states(database_url, "withings_sync"))
        assert scalar(
            database_url, "SELECT origin FROM jobs WHERE kind = 'withings_sync' "
            "ORDER BY created_at LIMIT 1"
        ) == "webhook"  # fmt: skip

        # Poll: the safety net queues a sync per completed stream, and they run.
        before = len(_states(database_url, "withings_sync"))
        await _queue(env, stored.id, "withings_poll")
        await _done(database_url, "withings_poll")
        assert _states(database_url, "withings_poll") == ["completed"]
        assert len(_states(database_url, "withings_sync")) > before

        # Reconcile: a complete pass over unchanged data deletes nothing.
        await _queue(env, stored.id, "withings_reconcile")
        await _done(database_url, "withings_reconcile")
        assert _states(database_url, "withings_reconcile") == ["completed"]
        assert measure_count() == len(expected)
        assert scalar(
            database_url, "SELECT count(*) FROM measure_groups WHERE deleted_at IS NOT NULL"
        ) == 0  # fmt: skip

    # Disconnect revokes upstream and drops the credentials; delete removes the imported rows.
    result = await disconnect_withings(
        env.factory, ring, ctx, stored.id, stepped_up=True, inner=inner
    )
    assert result.cleared is True
    assert result.revoke_failed is False
    assert "v2/oauth2:revoke" in [entry.endpoint for entry in fake.log]
    await purge_connection(env.factory, ctx, stored.id)
    assert measure_count() == 0
    assert scalar(database_url, "SELECT count(*) FROM connections WHERE id = :c", c=stored.id) == 0
    assert scalar(database_url, "SELECT count(*) FROM webhook_events") == 0
