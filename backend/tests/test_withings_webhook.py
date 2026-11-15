"""The webhook receiver and secret rotation (#24). Synthetic ids and key bytes only.

No real account, token, webhook secret or measurement. The pepper and seed here are labelled
filler (``\\x11`` and ``\\x22``), not key material from a file.
"""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from io import StringIO
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from jobtools import engine_for, ensure_user
from ownvitals.config import Settings
from ownvitals.db import create_engine as async_engine
from ownvitals.db import create_session_factory
from ownvitals.log import configure_logging
from ownvitals.withings.subscriptions import (
    CurrentWebhookSeed,
    RetiredWebhookSeed,
    callback_url,
    derive_secret,
    mint,
    pepper_hmac,
    subscribe,
    wrap_seed,
)
from ownvitals.withings.webhooks import (
    PREFIX,
    Cache,
    CachedEntry,
    Factory,
    Metrics,
    WebhookMiddleware,
    append_secret,
    retire_all,
    retire_secret,
    rotation_stalls,
)
from withingsschema import PEPPER, SEED, new_app, seed_keys

WHEN = datetime(2026, 6, 1, tzinfo=UTC)
SEED_BYTES = b"\x11" * 32
PEPPER_BYTES = b"\x22" * 32
USERID = 424242
MEDIA = "application/x-www-form-urlencoded"
SECRET_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
async def factory(app_database_url: str) -> AsyncIterator[Factory]:
    engine = async_engine(Settings(database_url=app_database_url))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def link(admin: Connection, app_id: int) -> UUID:
    owner = uuid4()
    ensure_user(admin, owner)
    row = uuid4()
    admin.execute(
        text(
            "INSERT INTO connections (id, owner_id, provider, status, public) "
            "VALUES (:id, :owner, 'withings', 'needs_reconnect', CAST(:public AS jsonb))"
        ),
        {
            "id": row,
            "owner": owner,
            "public": json.dumps(
                {"app_id": app_id, "withings_userid": USERID, "scopes": ["user.metrics"]}
            ),
        },
    )
    return row


def counts(admin: Connection) -> tuple[int, int]:
    jobs = admin.execute(text("SELECT count(*) FROM jobs")).scalar_one()
    events = admin.execute(text("SELECT count(*) FROM webhook_events")).scalar_one()
    return int(jobs), int(events)


def entry(app_id: int, secret_id: UUID = SECRET_ID) -> tuple[str, CachedEntry]:
    presented = derive_secret(SEED_BYTES, app_id, secret_id)
    cached = CachedEntry(
        app_id,
        str(secret_id),
        pepper_hmac(PEPPER_BYTES, presented),
        PEPPER_BYTES,
        WHEN - timedelta(days=1),
        None,
    )
    return presented, cached


def receiver(factory: Factory, cached: Cache) -> FastAPI:
    app = FastAPI()
    app.state.session_factory = factory
    app.state.webhook_cache = cached
    app.state.webhook_metrics = Metrics()
    app.state.webhook_clock = lambda: WHEN
    app.add_middleware(WebhookMiddleware)
    return app


def form(**overrides: int) -> bytes:
    values = {"userid": USERID, "appli": 1, "startdate": 1_700_000_000, "enddate": 1_700_000_100}
    values.update(overrides)
    return "&".join(f"{name}={values[name]}" for name in values).encode()


async def post(
    app: FastAPI, secret: str, body: bytes, *, content_type: str | None = MEDIA
) -> httpx.Response:
    headers = {} if content_type is None else {"content-type": content_type}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(PREFIX + secret, content=body, headers=headers)


@pytest.fixture
def captured() -> Iterator[StringIO]:
    stream = StringIO()
    configure_logging(stream=stream)
    try:
        yield stream
    finally:
        logging.getLogger().handlers.clear()


@pytest.mark.usefixtures("schema")
async def test_bad_notifications_create_no_job_and_do_not_log_the_secret(
    factory: Factory, database_url: str, captured: StringIO
) -> None:
    """Forged, oversized, malformed, replayed, wrong type, unknown appli, dates out of range."""
    with engine_for(database_url) as engine, engine.begin() as admin:
        app_id = new_app(database_url)
        presented, cached = entry(app_id)
        link(admin, app_id)
    app = receiver(factory, Cache(entries=[cached], read_at=WHEN))
    forged = "forged-secret-not-the-derived-one"
    window = form()
    same = [
        await post(app, presented, window, content_type="application/json"),
        await post(app, forged, window, content_type="application/json"),
        await post(app, presented, b"x" * (2048 + 1)),
        await post(app, forged, b"y" * (2048 + 1)),
        await post(app, presented, b"userid=1&appli=nope&startdate=1&enddate=2"),
        await post(app, forged, b"not-a-form"),
        await post(app, presented, form(appli=99)),
        await post(app, forged, form(appli=61)),
        await post(app, presented, form(startdate=1, enddate=2)),
        await post(app, forged, form(startdate=1_700_000_200, enddate=1_700_000_100)),
    ]
    assert [item.status_code for item in same[:2]] == [415, 415]
    assert [item.status_code for item in same[2:4]] == [413, 413]
    assert all(item.status_code == 400 for item in same[4:])
    accepted = await post(app, presented, window)
    replay = await post(app, presented, window)
    ignored = await post(app, forged, window)
    head = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=head, base_url="http://test") as client:
        head_reply = await client.head(PREFIX + presented)
    with engine_for(database_url) as engine, engine.connect() as admin:
        jobs, events = counts(admin)
    assert accepted.status_code == 200
    assert replay.status_code == 200
    assert ignored.status_code == 200
    assert head_reply.status_code == 200
    assert (jobs, events) == (1, 1)
    assert app.state.webhook_metrics.ignored == 1
    logged = captured.getvalue()
    assert presented not in logged
    assert forged not in logged
    assert "/api/webhooks/withings/[redacted]" in logged


@pytest.mark.usefixtures("schema")
async def test_a_crash_after_commit_leaves_one_event_and_redelivery_collapses(
    factory: Factory, database_url: str
) -> None:
    with engine_for(database_url) as engine, engine.begin() as admin:
        app_id = new_app(database_url)
        presented, cached = entry(app_id)
        link(admin, app_id)
    app = receiver(factory, Cache(entries=[cached], read_at=WHEN))
    app.state.webhook_crash_after_commit = True
    crashed = await post(app, presented, form())
    with engine_for(database_url) as engine, engine.connect() as admin:
        after_crash = counts(admin)
    again = await post(app, presented, form())
    with engine_for(database_url) as engine, engine.connect() as admin:
        after_retry = counts(admin)
    assert crashed.status_code == 500
    assert again.status_code == 200
    assert after_crash == (1, 1)
    assert after_retry == (1, 1)


@pytest.mark.usefixtures("schema")
async def test_both_secrets_work_during_overlap_and_the_retired_one_is_ignored(
    factory: Factory, database_url: str, captured: StringIO
) -> None:
    seed_keys(database_url)
    with engine_for(database_url) as engine, engine.begin() as admin:
        app_id = new_app(database_url)
        link(admin, app_id)
        admin.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = now(), "
                "rotation_reason = 'routine' WHERE id = :id"
            ),
            {"id": app_id},
        )
    seed = CurrentWebhookSeed(SEED, SEED_BYTES)
    first = await append_secret(
        factory,
        app_id=app_id,
        seed=seed,
        pepper=PEPPER_BYTES,
        pepper_id=PEPPER,
        active_from=WHEN - timedelta(minutes=1),
        now=WHEN - timedelta(minutes=2),
    )
    # The append cleared the request. A second live secret is the overlap.
    with engine_for(database_url) as engine, engine.begin() as admin:
        admin.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = now(), "
                "rotation_reason = 'routine' WHERE id = :id"
            ),
            {"id": app_id},
        )
    second = await append_secret(
        factory,
        app_id=app_id,
        seed=seed,
        pepper=PEPPER_BYTES,
        pepper_id=PEPPER,
        active_from=WHEN - timedelta(minutes=1),
        now=WHEN - timedelta(minutes=2),
    )
    stored = _stored(database_url, app_id)
    cache = Cache(
        entries=[_cached(app_id, item, PEPPER_BYTES) for item in stored],
        read_at=WHEN,
    )
    app = receiver(factory, cache)
    assert (await post(app, first, form(enddate=1_700_000_100))).status_code == 200
    assert (await post(app, second, form(enddate=1_700_000_200))).status_code == 200
    assert app.state.webhook_metrics.ignored == 0
    retired_id = UUID(str(stored[0]["secret_id"]))
    await retire_secret(factory, app_id=app_id, secret_id=retired_id)
    after = _stored(database_url, app_id)
    retired = next(item for item in after if item["secret_id"] == str(retired_id))
    assert retired["retire_at"] is not None
    app.state.webhook_cache = Cache(
        entries=[_cached(app_id, item, PEPPER_BYTES) for item in after], read_at=WHEN
    )
    ignored = await post(app, first, form(enddate=1_700_000_300))
    still = await post(app, second, form(enddate=1_700_000_400))
    with engine_for(database_url) as engine, engine.connect() as admin:
        jobs, _events = counts(admin)
    assert ignored.status_code == 200
    assert still.status_code == 200
    assert app.state.webhook_metrics.ignored == 1
    assert jobs == 1
    logged = captured.getvalue()
    assert first not in logged
    assert second not in logged


def _stored(url: str, app_id: int) -> list[dict[str, object]]:
    with engine_for(url) as engine, engine.connect() as conn:
        raw = conn.execute(
            text("SELECT webhook_secrets FROM withings_apps WHERE id = :id"), {"id": app_id}
        ).scalar_one()
    if isinstance(raw, str):
        raw = json.loads(raw)
    return list(raw)


def _set_active_from(url: str, app_id: int, secret_id: str, active_from: datetime) -> None:
    stamp = active_from.astimezone(UTC).isoformat()
    with engine_for(url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE withings_apps SET webhook_secrets = ("
                " SELECT coalesce(jsonb_agg(CASE WHEN e->>'secret_id' = :secret"
                " THEN jsonb_set(e, '{active_from}', to_jsonb(CAST(:stamp AS text)))"
                " ELSE e END), '[]'::jsonb)"
                " FROM jsonb_array_elements(webhook_secrets) e) WHERE id = :id"
            ),
            {"secret": secret_id, "stamp": stamp, "id": app_id},
        )


def _request_rotation(url: str, app_id: int) -> None:
    with engine_for(url) as engine, engine.begin() as admin:
        admin.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = now(), "
                "rotation_reason = 'routine' WHERE id = :id"
            ),
            {"id": app_id},
        )


def _cached(app_id: int, item: dict[str, object], pepper: bytes) -> CachedEntry:
    opened = datetime.fromisoformat(str(item["active_from"]))
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=UTC)
    retired = item["retire_at"]
    closed = None if retired is None else datetime.fromisoformat(str(retired))
    if isinstance(closed, datetime) and closed.tzinfo is None:
        closed = closed.replace(tzinfo=UTC)
    return CachedEntry(
        app_id,
        str(item["secret_id"]),
        bytes.fromhex(str(item["hmac"])),
        pepper,
        opened,
        closed,
    )


@pytest.mark.usefixtures("schema")
async def test_a_held_lock_and_cancelled_repoint_jobs_raise_the_stall_alert(
    factory: Factory, database_url: str, app_database_url: str
) -> None:
    seed_keys(database_url)
    with engine_for(database_url) as engine, engine.begin() as admin:
        app_id = new_app(database_url)
        row = link(admin, app_id)
        admin.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = now() - interval '1 minute', "
                "rotation_reason = 'compromise' WHERE id = :id"
            ),
            {"id": app_id},
        )
    held = create_engine(database_url)
    try:
        with held.connect() as locker:
            locker.execute(text("SELECT pg_advisory_lock(4, :app)"), {"app": app_id})
            try:
                locked = await rotation_stalls(factory, app_id, bound=timedelta(0))
            finally:
                locker.execute(text("SELECT pg_advisory_unlock(4, :app)"), {"app": app_id})
    finally:
        held.dispose()
    assert "rotation_locked" in locked
    presented = await append_secret(
        factory,
        app_id=app_id,
        seed=CurrentWebhookSeed(SEED, SEED_BYTES),
        pepper=PEPPER_BYTES,
        pepper_id=PEPPER,
        active_from=WHEN - timedelta(minutes=1),
        now=WHEN - timedelta(minutes=2),
    )
    secret_id = UUID(str(_stored(database_url, app_id)[-1]["secret_id"]))
    with engine_for(app_database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "SELECT public.queue_enqueue(:connection, 'notify_update', 'scheduler', "
                "CAST(:payload AS jsonb), NULL, 3, :secret)"
            ),
            {
                "connection": row,
                "payload": json.dumps({"connection_id": str(row), "secret_id": str(secret_id)}),
                "secret": secret_id,
            },
        )
    with engine_for(database_url) as engine, engine.begin() as admin:
        admin.execute(
            text(
                "UPDATE withings_apps SET rotation_requested_at = now(), "
                "rotation_reason = 'compromise' WHERE id = :id"
            ),
            {"id": app_id},
        )
    cancelled = await retire_all(factory, app_id=app_id)
    with engine_for(database_url) as engine, engine.connect() as admin:
        state = admin.execute(
            text("SELECT state FROM jobs WHERE target_secret_id = :secret"),
            {"secret": secret_id},
        ).scalar_one()
    assert cancelled == 1
    assert state == "cancelled"
    assert presented not in state


@pytest.mark.usefixtures("schema")
async def test_step_late_follows_the_newest_secret_not_the_original(
    factory: Factory, database_url: str
) -> None:
    """A healthy overlap keeps the original secret. step_late waits for the newest one."""
    seed_keys(database_url)
    bound = timedelta(hours=1)
    now = datetime.now(UTC)
    app_id = new_app(database_url)
    seed = CurrentWebhookSeed(SEED, SEED_BYTES)
    _request_rotation(database_url, app_id)
    await append_secret(
        factory,
        app_id=app_id,
        seed=seed,
        pepper=PEPPER_BYTES,
        pepper_id=PEPPER,
        active_from=now - bound - timedelta(hours=1),
        now=now - bound - timedelta(hours=2),
    )
    _request_rotation(database_url, app_id)
    await append_secret(
        factory,
        app_id=app_id,
        seed=seed,
        pepper=PEPPER_BYTES,
        pepper_id=PEPPER,
        active_from=now + timedelta(hours=1),
    )
    stored = _stored(database_url, app_id)
    newest = max(stored, key=lambda item: str(item["active_from"]))
    healthy = await rotation_stalls(factory, app_id, bound=bound)
    assert "step_late" not in healthy
    _set_active_from(
        database_url,
        app_id,
        str(newest["secret_id"]),
        datetime.now(UTC) - bound + timedelta(minutes=5),
    )
    recent = await rotation_stalls(factory, app_id, bound=bound)
    assert "step_late" not in recent
    _set_active_from(database_url, app_id, str(newest["secret_id"]), datetime.now(UTC) - bound)
    stalled = await rotation_stalls(factory, app_id, bound=bound)
    assert "step_late" in stalled


def test_a_retired_seed_rebuilds_an_old_url_and_cannot_mint() -> None:
    current = wrap_seed(SEED, SEED_BYTES, current_id=SEED)
    retired = wrap_seed("seed-2", SEED_BYTES, current_id=SEED)
    secret_id = uuid4()
    assert isinstance(current, CurrentWebhookSeed)
    assert isinstance(retired, RetiredWebhookSeed)
    minted = mint(current, 7, secret_id)
    assert retired.rebuild(7, secret_id) == derive_secret(SEED_BYTES, 7, secret_id)
    with pytest.raises(TypeError):
        mint(retired, 7, uuid4())  # pyright: ignore[reportArgumentType]
    assert minted == current.derive(7, secret_id)
    assert "derive" not in dir(retired)


class _FakeNotify:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def notify_subscribe(
        self,
        token: str,
        *,
        callbackurl: str,
        appli: int,
        comment: str | None = None,
        **rest: object,
    ) -> None:
        self.calls.append((appli, callbackurl))


async def test_subscribe_puts_the_secret_only_in_the_callback_argument() -> None:
    secret = derive_secret(SEED_BYTES, 3, SECRET_ID)
    client = _FakeNotify()
    await subscribe(client, "synthetic-token", "https://example.test", secret, (1, 44))
    assert client.calls == [
        (1, callback_url("https://example.test", secret)),
        (44, callback_url("https://example.test/", secret)),
    ]
    assert all(
        url.startswith("https://example.test/api/webhooks/withings/") for _, url in client.calls
    )


@pytest.mark.usefixtures("schema")
async def test_a_cold_cache_refuses_every_post_and_head_stays_200(factory: Factory) -> None:
    app = receiver(factory, Cache())
    cold = await post(app, "anything", form())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        head = await client.head(PREFIX + "anything")
    assert cold.status_code == 503
    assert head.status_code == 200
