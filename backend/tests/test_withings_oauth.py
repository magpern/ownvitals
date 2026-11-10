"""Connect, OAuth state and the owner-role app commands, against the fake. Synthetic only."""

import asyncio
import json
import urllib.request
from io import StringIO
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from ownvitals.egress import OutboundBlocked, core, exchange_withings_code, install_worker_allowlist
from ownvitals.withings.apps import ROTATE, create_app, request_rotation, run_withings_apps
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.oauth import (
    SessionProof,
    StateStore,
    callback,
    revoke_stale_subscriptions,
    start,
)
from ownvitals.withings.oauth_state import STATE_TTL_SECONDS
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingswire import MASKED, Request, Subscription

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")
CLIENT = "synthetic-client"
SECRET = "synthetic-secret"
BASE = "https://app.example.test"
REDIRECT = BASE + "/api/oauth/withings/callback"
CURRENT = BASE + "/api/webhooks/withings/current"
STALE = BASE + "/api/webhooks/withings/stale-marker"
OTHER = "https://other.example.test/api/webhooks/withings/other"


@pytest.fixture(autouse=True)
def _web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


@pytest.fixture
async def owner(owner_database_url: str):
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _fake() -> FakeWithings:
    fake = FakeWithings(seed=5)
    fake.add_app(CLIENT, SECRET, (REDIRECT,))
    fake.add_user()
    return fake


async def _app(env: Env, owner: object) -> int:
    return await create_app(owner, env.factory, env.app.state.keyring, CLIENT, BASE, SECRET)  # type: ignore[arg-type]


def _proof(stepped_up: bool = True) -> SessionProof:
    return SessionProof(uuid4(), stepped_up)


async def _code(fake: FakeWithings, url: str) -> str:
    page = fake.handle(Request("GET", url))
    return parse_qs(urlsplit(page.headers["location"]).query)["code"][0]


async def test_start_requires_step_up(env: Env, owner: object) -> None:
    app_id = await _app(env, owner)
    ctx = make_context(await env.user("step@example.com"))
    store = StateStore()
    with pytest.raises(Exception, match="step_up_required") as caught:
        await start(env.factory, env.app.state.keyring, ctx, _proof(False), app_id, store=store)
    assert caught.value.reason == "step_up_required"  # type: ignore[attr-defined]


async def test_callback_refuses_a_replay_and_a_foreign_state(env: Env, owner: object) -> None:
    app_id = await _app(env, owner)
    ctx = make_context(await env.user("state@example.com"))
    fake = _fake()
    ring = env.app.state.keyring
    store = StateStore()
    proof = _proof()
    started = await start(env.factory, ring, ctx, proof, app_id, store=store)
    code = await _code(fake, started.authorize_url)
    inner = FakeWithingsTransport(fake)
    await callback(env.factory, ring, ctx, proof, code, started.state, store=store, inner=inner)
    with pytest.raises(Exception, match="state_rejected"):
        await callback(env.factory, ring, ctx, proof, code, started.state, store=store, inner=inner)
    foreign = _proof()
    other = await start(env.factory, ring, ctx, proof, app_id, store=store)
    with pytest.raises(Exception, match="state_rejected"):
        await callback(
            env.factory, ring, ctx, foreign, "synthetic-code", other.state, store=store, inner=inner
        )
    assert sum(1 for entry in fake.log if entry.endpoint == "v2/oauth2:requesttoken") == 1


async def test_an_expired_state_is_refused(env: Env, owner: object) -> None:
    """Same session, so only the age decides: accepted just inside the 10 minutes, refused at
    them, before any Withings call."""
    app_id = await _app(env, owner)
    ring = env.app.state.keyring
    ctx = make_context(await env.user("ttl@example.com"))
    clock = {"now": 0.0}
    store = StateStore(clock=lambda: clock["now"])
    proof = _proof()
    fake = _fake()
    inner = FakeWithingsTransport(fake)
    fresh = await start(env.factory, ring, ctx, proof, app_id, store=store)
    late = await start(env.factory, ring, ctx, proof, app_id, store=store)
    code = await _code(fake, fresh.authorize_url)
    fake.log.clear()
    clock["now"] = STATE_TTL_SECONDS - 1.0
    await callback(env.factory, ring, ctx, proof, code, fresh.state, store=store, inner=inner)
    assert [entry.endpoint for entry in fake.log] == ["v2/oauth2:requesttoken"]
    fake.log.clear()
    clock["now"] = float(STATE_TTL_SECONDS)
    with pytest.raises(Exception, match="state_rejected"):
        await callback(
            env.factory, ring, ctx, proof, "synthetic-code", late.state, store=store, inner=inner
        )
    assert fake.log == []


async def test_the_state_expiry_has_one_boundary() -> None:
    """The store alone, synthetic ids: a state taken just inside its 10 minutes opens once, one
    taken at them is refused and is no longer held (#311: one expiry check, not two)."""
    clock = {"now": 0.0}
    store = StateStore(clock=lambda: clock["now"])
    session_id, user_id, uri = uuid4(), uuid4(), f"{BASE}/synthetic-callback"
    inside = await store.issue(session_id=session_id, user_id=user_id, app_id=1, redirect_uri=uri)
    at = await store.issue(session_id=session_id, user_id=user_id, app_id=1, redirect_uri=uri)
    assert inside is not None
    assert at is not None
    clock["now"] = STATE_TTL_SECONDS - 0.001
    assert await store.take(inside) is not None
    assert await store.take(inside) is None
    clock["now"] = float(STATE_TTL_SECONDS)
    assert await store.take(at) is None
    assert store._rows == {}


async def test_a_withings_user_linked_elsewhere_is_refused(env: Env, owner: object) -> None:
    app_id = await _app(env, owner)
    ring = env.app.state.keyring
    fake = _fake()
    first = make_context(await env.user("owner-a@example.com"))
    second = make_context(await env.user("owner-b@example.com"))
    store = StateStore()
    proof_a, proof_b = _proof(), _proof()
    started = await start(env.factory, ring, first, proof_a, app_id, store=store)
    inner = FakeWithingsTransport(fake)
    await callback(
        env.factory,
        ring,
        first,
        proof_a,
        await _code(fake, started.authorize_url),
        started.state,
        store=store,
        inner=inner,
    )
    again = await start(env.factory, ring, second, proof_b, app_id, store=store)
    with pytest.raises(Exception, match="linked_elsewhere") as caught:
        await callback(
            env.factory,
            ring,
            second,
            proof_b,
            await _code(fake, again.authorize_url),
            again.state,
            store=store,
            inner=inner,
        )
    assert caught.value.reason == "linked_elsewhere"  # type: ignore[attr-defined]


async def test_a_different_live_withings_account_is_refused(env: Env, owner: object) -> None:
    app_id = await _app(env, owner)
    ring = env.app.state.keyring
    fake = _fake()
    other = fake.add_user()
    ctx = make_context(await env.user("one-live@example.com"))
    store = StateStore()
    proof = _proof()
    inner = FakeWithingsTransport(fake)
    started = await start(env.factory, ring, ctx, proof, app_id, store=store)
    await callback(
        env.factory,
        ring,
        ctx,
        proof,
        await _code(fake, started.authorize_url),
        started.state,
        store=store,
        inner=inner,
    )
    fake.consenting_userid = other.userid
    nxt = await start(env.factory, ring, ctx, proof, app_id, store=store)
    with pytest.raises(Exception, match="other_withings_account"):
        await callback(
            env.factory,
            ring,
            ctx,
            proof,
            await _code(fake, nxt.authorize_url),
            nxt.state,
            store=store,
            inner=inner,
        )


async def test_connect_urls_never_carry_a_secret(
    env: Env, owner: object, caplog: pytest.LogCaptureFixture
) -> None:
    app_id = await _app(env, owner)
    ring = env.app.state.keyring
    fake = _fake()
    ctx = make_context(await env.user("urls@example.com"))
    store = StateStore()
    proof = _proof()
    inner = FakeWithingsTransport(fake)
    started = await start(env.factory, ring, ctx, proof, app_id, store=store)
    assert "client_secret" not in started.authorize_url
    code = await _code(fake, started.authorize_url)
    stored = await callback(
        env.factory, ring, ctx, proof, code, started.state, store=store, inner=inner
    )
    from ownvitals.egress import install_worker_allowlist

    install_worker_allowlist(ai_enabled=False)
    userid = next(iter(fake.users))
    fake.subscriptions.extend(
        (
            Subscription(CLIENT, userid, 1, CURRENT),
            Subscription(CLIENT, userid, 1, STALE),
            Subscription(CLIENT, userid, 1, OTHER),
        )
    )
    caplog.set_level("DEBUG")
    revoked, kept = await revoke_stale_subscriptions(
        env.factory, ring, ctx, stored.id, current=CURRENT, inner=inner
    )
    assert (revoked, kept) == (1, 1)
    assert [item.callbackurl for item in fake.subscriptions] == [CURRENT, OTHER]
    assert fake.log
    for entry in fake.log:
        assert set(entry.query_names).isdisjoint(MASKED)
    assert any(entry.query_names for entry in fake.log)
    assert "stale-marker" not in caplog.text
    async with env.db() as db:
        # system session: withings rows are hidden; the public JSON is read as the owner below
        pass
    from ownvitals.db import user_session

    async with user_session(env.factory, ctx) as db:
        public = (
            await db.execute(
                text("SELECT public::text FROM connections WHERE id = :id"), {"id": stored.id}
            )
        ).scalar_one()
    assert "stale-marker" not in public
    assert "callbackurl" not in public


async def test_web_cannot_refresh_revoke_or_call_another_withings_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake()
    inner = FakeWithingsTransport(fake)
    app = WithingsApp(CLIENT, SECRET)
    token = "synthetic-access-token-aaaaaaaa"
    calls = {
        "refresh": lambda c: c.refresh("synthetic-refresh-token-bbbbbbb"),
        "revoke": lambda c: c.revoke(next(iter(fake.users))),
        "notify_list": lambda c: c.notify_list(token),
        "notify_revoke": lambda c: c.notify_revoke(token, callbackurl=CURRENT, appli=1),
        "getmeas": lambda c: c.getmeas(token),
    }
    # In web the worker's client cannot be built at all.
    with pytest.raises(OutboundBlocked):
        async with WithingsClient.open(app, inner=inner):
            pass
    # A client built while the worker list was installed still sends nothing once it is gone:
    # each call is refused by its transport, before the fake sees a request.
    install_worker_allowlist(ai_enabled=False)
    async with WithingsClient.open(app, inner=inner) as client:
        monkeypatch.setattr(core, "_worker_list", None)
        for name, call in calls.items():
            with pytest.raises(OutboundBlocked):
                await call(client)
            assert fake.log == [], name
    code = fake.authorize(CLIENT, next(iter(fake.users)), REDIRECT)
    response = await exchange_withings_code(
        client_id=CLIENT, client_secret=SECRET, code=code, redirect_uri=REDIRECT, inner=inner
    )
    assert response.status_code == 200
    assert json.loads(response.content)["status"] == 0


async def test_the_app_role_cannot_write_withings_apps(env: Env) -> None:
    engine = env.factory.kw["bind"]
    statements = (
        "INSERT INTO withings_apps (client_id) VALUES ('synthetic-client')",
        "UPDATE withings_apps SET client_id = client_id",
        "DELETE FROM withings_apps",
    )
    async with engine.connect() as conn:  # type: ignore[union-attr]
        for statement in statements:
            with pytest.raises(DBAPIError, match="permission denied"):
                await conn.execute(text(statement))
            await conn.rollback()


async def test_rotate_never_downgrades_a_pending_compromise(env: Env, owner: object) -> None:
    app_id = await _app(env, owner)
    assert "select" not in ROTATE.text.lower()
    assert ROTATE.text.lower().count("update") == 1
    assert await request_rotation(owner, app_id, "compromise") == "compromise"  # type: ignore[arg-type]
    assert await request_rotation(owner, app_id, "routine") == "compromise"  # type: ignore[arg-type]
    reason = await env.scalar("SELECT rotation_reason FROM withings_apps WHERE id = :id", id=app_id)
    secrets = await env.scalar(
        "SELECT webhook_secrets::text FROM withings_apps WHERE id = :id", id=app_id
    )
    assert reason == "compromise"
    assert secrets == "[]"
    fresh = await _app(env, owner)
    first = await owner.connect()  # type: ignore[attr-defined]
    second = await owner.connect()  # type: ignore[attr-defined]
    try:
        await first.begin()
        await first.execute(ROTATE, {"id": fresh, "reason": "compromise"})

        async def routine() -> str:
            async with second.begin():
                stored = (
                    await second.execute(ROTATE, {"id": fresh, "reason": "routine"})
                ).scalar_one()
            return str(stored)

        task = asyncio.create_task(routine())
        await asyncio.sleep(0.3)
        await first.commit()
        assert await asyncio.wait_for(task, 5) == "compromise"
    finally:
        await first.close()
        await second.close()
    assert (
        await env.scalar("SELECT rotation_reason FROM withings_apps WHERE id = :id", id=fresh)
        == "compromise"
    )


async def test_the_operator_command_prints_no_secret(
    env: Env, owner_database_url: str, tmp_path
) -> None:
    path = tmp_path / "client-secret"
    path.write_text(SECRET + "\n", encoding="ascii")
    settings = env.settings.model_copy(
        update={"migrations_database_url": SecretStr(owner_database_url)}
    )
    args = type("A", (), {})()
    args.action = "create"  # type: ignore[attr-defined]
    args.client_id = CLIENT  # type: ignore[attr-defined]
    args.callback_base = BASE  # type: ignore[attr-defined]
    args.secret_file = str(path)  # type: ignore[attr-defined]
    out, err = StringIO(), StringIO()
    assert await run_withings_apps(args, settings, out, err) == 0  # type: ignore[arg-type]
    assert SECRET not in out.getvalue()
    assert SECRET not in err.getvalue()
    assert out.getvalue().strip().isdigit()
