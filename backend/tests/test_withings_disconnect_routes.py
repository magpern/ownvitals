"""The disconnect and account-delete routes on the revoke-first path (#163). Synthetic only.

The routes enqueue a ``withings_disconnect`` job and wait for it; the revoke runs in the job
(here an in-process worker over the fake), never in the route. Every token, session and
Withings account below is synthetic.
"""

import asyncio
import contextlib
import json
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from flows import Browser, only_user_id, reason, signed_in
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.auth.purge import (
    JOB_SLACK,
    RECENT_AUTH_WINDOW,
    STEPUP_WINDOW,
    disconnect_kind,
    finish_disconnect,
    register_disconnect_kind,
)
from ownvitals.auth.sessions import RECENT_AUTH_WINDOW as SESSIONS_RECENT_AUTH
from ownvitals.auth.sessions import STEPUP_WINDOW as SESSIONS_STEPUP
from ownvitals.connections import routes as connection_routes
from ownvitals.connections.providers.withings import store_grant
from ownvitals.context import UserContext
from ownvitals.egress import core, install_worker_allowlist
from ownvitals.jobs import KindRegistry, queue, service
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.withings.disconnect import DISCONNECT_KIND, REMOVAL, DisconnectInput
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.oauth import StateStore, callback, start
from test_withings_disconnect import (
    CLIENT,
    CURRENT,
    FOREIGN,
    OLD,
    _app,
    _asleep,
    _code,
    _fake,
    _hits,
    _proof,
    _run,
    _scalar,
)
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingswire import Failure, Subscription

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")
_CREDENTIALS = (
    "SELECT count(*) FROM connections WHERE id = :c AND secret IS NOT NULL "
    "AND wrapped_dek IS NOT NULL"
)


@pytest.fixture(autouse=True)
def _web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    monkeypatch.setattr(connection_routes, "REVOKE_WAIT_SECONDS", 15.0)
    monkeypatch.setattr(connection_routes, "_POLL_SECONDS", 0.05)


@pytest.fixture
async def owner(owner_database_url: str):
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


class Spy(FakeWithingsTransport):
    """The fake, recording for each revoke whether the row still held its credentials."""

    def __init__(self, fake: FakeWithings, database_url: str) -> None:
        super().__init__(fake)
        self.database_url = database_url
        self.connection_id: UUID | None = None
        self.revokes: list[tuple[str, bool]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = parse_qs((await request.aread()).decode())
        action = body.get("action", [""])[0]
        if action == "revoke" and self.connection_id is not None:
            held = _scalar(self.database_url, _CREDENTIALS, c=self.connection_id) == 1
            self.revokes.append((request.url.path.strip("/"), held))
        return await super().handle_async_request(request)


@dataclass
class Linked:
    browser: Browser
    ctx: UserContext
    connection_id: UUID
    fake: FakeWithings
    spy: Spy
    app_id: int
    userid: int
    access: str


async def _linked(env: Env, owner: object, database_url: str) -> Linked:
    """A signed-in, stepped-up user with a Withings connection on the fake."""
    browser = await signed_in(env)
    ctx = make_context(await only_user_id(env))
    app_id = await _app(env, owner)
    ring = env.app.state.keyring
    fake = _fake()
    store = StateStore()
    proof = _proof()
    spy = Spy(fake, database_url)
    started = await start(env.factory, ring, ctx, proof, app_id, store=store)
    stored = await callback(
        env.factory,
        ring,
        ctx,
        proof,
        await _code(fake, started.authorize_url),
        started.state,
        store=store,
        inner=spy,
    )
    userid = next(iter(fake.users))
    fake.subscriptions.extend(
        (
            Subscription(CLIENT, userid, 1, CURRENT),
            Subscription(CLIENT, userid, 44, OLD),
            Subscription(CLIENT, userid, 1, FOREIGN),
        )
    )
    spy.connection_id = stored.id
    fake.log.clear()
    access = fake.grant(CLIENT, userid).access
    return Linked(browser, ctx, stored.id, fake, spy, app_id, userid, access)


@contextlib.asynccontextmanager
async def worker(env: Env, linked: Linked) -> AsyncIterator[None]:
    """An in-process worker that runs only ``withings_disconnect``, over the fake."""
    install_worker_allowlist(ai_enabled=False)
    registry = KindRegistry()
    registry.register(
        disconnect_kind(
            ring=env.app.state.keyring,
            inner=linked.spy,
            budget=lambda: RetryBudget(retries=2),
            sleep=_asleep,
        )
    )
    stop = asyncio.Event()
    config = RunConfig(lease_seconds=60, accounts=ACCOUNTS)

    async def loop() -> None:
        while not stop.is_set():
            row = await queue.claim(env.factory, "synthetic-worker", 60, registry.names())
            if row is None:
                await asyncio.sleep(0.05)
                continue
            await execute(Execution(row), env.factory, registry, config, asyncio.Event())

    task = asyncio.ensure_future(loop())
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


def _events(url: str, app_id: int, userid: int) -> None:
    """Two synthetic events: this user's and another Withings user's. The table is a system
    table the account fixtures do not empty."""
    _run(url, "DELETE FROM webhook_events WHERE dedupe_key LIKE 'synthetic-route-event-%'")
    _run(
        url,
        "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, enddate, "
        "dedupe_key) VALUES (:a, :u, 1, 1, 2, 'synthetic-route-event-a')",
        a=app_id,
        u=userid,
    )
    _run(
        url,
        "INSERT INTO webhook_events (app_id, withings_userid, appli, startdate, enddate, "
        "dedupe_key) VALUES (:a, 2002, 1, 1, 2, 'synthetic-route-event-b')",
        a=app_id,
    )


def _event_count(url: str, key: str) -> object:
    return _scalar(url, "SELECT count(*) FROM webhook_events WHERE dedupe_key = :k", k=key)


def _job_states(url: str, owner_id: UUID) -> list[tuple[object, object]]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT state, error_category FROM jobs WHERE owner_id = :o AND kind = :k"),
                {"o": owner_id, "k": DISCONNECT_KIND},
            ).all()
            return [(row.state, row.error_category) for row in rows]
    finally:
        engine.dispose()


# --- acceptance 1: both routes revoke first, in the worker -------------------------------------


async def test_disconnect_route_revokes_in_the_worker_before_credentials_clear(
    env: Env, owner: object, database_url: str
) -> None:
    linked = await _linked(env, owner, database_url)
    async with worker(env, linked):
        response = await linked.browser.request("DELETE", "/api/connections/withings")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "disconnected",
        "revoke_failed": False,
        "removal": None,
        "imported_deleted": False,
    }
    assert linked.spy.revokes == [("notify", True), ("notify", True), ("v2/oauth2", True)]
    assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 0
    assert _job_states(database_url, linked.ctx.user_id) == [("completed", None)]
    listed = (await linked.browser.get("/api/connections")).json()["connections"]
    assert [(row["status"], row["last_error"]) for row in listed] == [("disconnected", None)]
    assert linked.access not in response.text
    assert _hits(database_url, linked.access) == []


async def test_account_delete_route_revokes_in_the_worker_before_the_account_goes(
    env: Env, owner: object, database_url: str
) -> None:
    linked = await _linked(env, owner, database_url)
    _events(database_url, linked.app_id, linked.userid)
    async with worker(env, linked):
        response = await linked.browser.request("DELETE", "/api/auth/account")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "deleted", "revoke_failed": False, "removal": None}
    assert linked.spy.revokes == [("notify", True), ("notify", True), ("v2/oauth2", True)]
    assert _scalar(database_url, "SELECT count(*) FROM users") == 0
    assert _scalar(database_url, "SELECT count(*) FROM connections") == 0
    assert _event_count(database_url, "synthetic-route-event-a") == 0
    assert _event_count(database_url, "synthetic-route-event-b") == 1
    assert not linked.browser.signed_in


# --- acceptance 2 and 3: no Withings call from web; revoke_failed reaches the caller ------------


async def test_without_a_worker_web_calls_nothing_and_reports_the_revoke_failed(
    env: Env, owner: object, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worker runs the kind, and the process is not the worker (a Withings client would be
    refused): the route makes no provider call, clears the credentials itself and says so."""
    monkeypatch.setattr(connection_routes, "REVOKE_WAIT_SECONDS", 0.3)
    linked = await _linked(env, owner, database_url)
    response = await linked.browser.request("DELETE", "/api/connections/withings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["revoke_failed"], body["removal"]) == (True, REMOVAL)
    assert linked.fake.log == []
    assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 0
    assert _job_states(database_url, linked.ctx.user_id) == [("cancelled", None)]
    listed = (await linked.browser.get("/api/connections")).json()["connections"]
    assert [(row["status"], row["last_error"]) for row in listed] == [
        ("disconnected", "revoke_failed")
    ]


async def test_a_failed_upstream_revoke_reaches_the_response_and_the_status(
    env: Env, owner: object, database_url: str
) -> None:
    linked = await _linked(env, owner, database_url)
    linked.fake.failures.append(
        Failure(kind="http", at=frozenset({1}), endpoint="v2/oauth2:revoke", http_status=400)
    )
    async with worker(env, linked):
        response = await linked.browser.request("DELETE", "/api/connections/withings")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["revoke_failed"], body["removal"]) == (True, REMOVAL)
    assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 0
    listed = (await linked.browser.get("/api/connections")).json()["connections"]
    assert [(row["status"], row["last_error"]) for row in listed] == [
        ("disconnected", "revoke_failed")
    ]


async def test_account_delete_reports_a_failed_revoke(
    env: Env, owner: object, database_url: str
) -> None:
    linked = await _linked(env, owner, database_url)
    linked.fake.failures.append(
        Failure(kind="http", at=frozenset({1}), endpoint="notify:list", http_status=400)
    )
    async with worker(env, linked):
        response = await linked.browser.request("DELETE", "/api/auth/account")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "deleted", "revoke_failed": True, "removal": REMOVAL}
    assert _scalar(database_url, "SELECT count(*) FROM users") == 0


# --- acceptance 4: "delete imported data" purges webhook events before the row goes -------------


async def test_delete_imported_purges_rows_and_webhook_events_then_the_row(
    env: Env, owner: object, database_url: str
) -> None:
    linked = await _linked(env, owner, database_url)
    other = make_context(await env.user("other-route@example.com"))
    await store_grant(
        env.factory,
        env.app.state.keyring,
        other,
        linked.app_id,
        2002,
        "user.metrics",
        "synthetic-access-token-route-b",
        "synthetic-refresh-token-route-b",
        expires_in=10800,
    )
    _events(database_url, linked.app_id, linked.userid)
    _run(
        database_url,
        "INSERT INTO devices (owner_id, connection_id, hash_deviceid) "
        "VALUES (:o, :c, 'synthetic-device')",
        o=linked.ctx.user_id,
        c=linked.connection_id,
    )
    async with worker(env, linked):
        response = await linked.browser.request(
            "DELETE", "/api/connections/withings?delete_imported=true"
        )
    assert response.status_code == 200, response.text
    assert response.json()["imported_deleted"] is True
    assert response.json()["revoke_failed"] is False
    assert _event_count(database_url, "synthetic-route-event-a") == 0
    assert _event_count(database_url, "synthetic-route-event-b") == 1
    owned = "SELECT count(*) FROM {} WHERE owner_id = :o"
    for table in ("devices", "connections", "jobs"):
        assert _scalar(database_url, owned.format(table), o=linked.ctx.user_id) == 0, table
    assert _scalar(database_url, owned.format("connections"), o=other.user_id) == 1


async def test_finish_disconnect_purges_webhook_events_before_the_row(
    env: Env, owner: object, database_url: str
) -> None:
    """The in-process path of #161 with the carried gap closed."""
    linked = await _linked(env, owner, database_url)
    install_worker_allowlist(ai_enabled=False)
    _events(database_url, linked.app_id, linked.userid)
    await finish_disconnect(
        env.factory,
        env.app.state.keyring,
        linked.ctx,
        linked.connection_id,
        stepped_up=True,
        delete_imported=True,
        inner=linked.spy,
    )
    assert _event_count(database_url, "synthetic-route-event-a") == 0
    assert _event_count(database_url, "synthetic-route-event-b") == 1
    assert (
        _scalar(
            database_url, "SELECT count(*) FROM connections WHERE id = :c", c=linked.connection_id
        )
        == 0
    )


# --- the step-up and refusals -------------------------------------------------------------------


async def test_the_routes_need_a_step_up_and_enqueue_nothing_without_it(
    env: Env, owner: object, database_url: str
) -> None:
    linked = await _linked(env, owner, database_url)
    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    for path in ("/api/connections/withings", "/api/auth/account"):
        refused = await linked.browser.request("DELETE", path)
        assert (refused.status_code, reason(refused)) == (403, "step_up_required"), path
    assert _job_states(database_url, linked.ctx.user_id) == []
    assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 1


async def test_a_job_without_a_stepped_up_session_revokes_nothing(
    env: Env, owner: object, database_url: str
) -> None:
    """A ``withings_disconnect`` job created by any other path (another user's session, an
    unknown one, or one whose step-up is too old) fails before any provider call."""
    linked = await _linked(env, owner, database_url)
    session_id = UUID(str(await env.scalar("SELECT id FROM sessions")))
    stranger = await env.user("stranger@example.com")
    foreign = (await env.login(stranger, mfa=True)).id
    await env.run(
        "UPDATE sessions SET reauth_at = now(), stepup_at = now(), mfa_at = now() WHERE id = :s",
        s=foreign,
    )
    old = timedelta(seconds=(STEPUP_WINDOW + JOB_SLACK).total_seconds() + 60)
    await env.run(
        "UPDATE sessions SET stepup_at = now() - :old WHERE id = :s", s=session_id, old=old
    )
    for target in (uuid4(), foreign, session_id):
        new = connection_routes._job(linked.connection_id, target)  # pyright: ignore[reportPrivateUsage]
        assert json.loads(new.input) == {
            "connection_id": str(linked.connection_id),
            "session_id": str(target),
        }
        created = await service.create(env.factory, linked.ctx, new, limit=5)
        assert created.job is not None
    async with worker(env, linked):
        for _ in range(200):
            states = _job_states(database_url, linked.ctx.user_id)
            if all(state != "queued" and state != "running" for state, _ in states):
                break
            await asyncio.sleep(0.05)
    assert _job_states(database_url, linked.ctx.user_id) == [("failed", "invalid_input")] * 3
    assert linked.fake.log == []
    assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 1


async def test_disconnect_without_a_live_withings_row_is_not_connected(env: Env) -> None:
    browser = await signed_in(env)
    refused = await browser.request("DELETE", "/api/connections/withings")
    assert (refused.status_code, reason(refused)) == (409, "not_connected")


async def test_the_kind_is_left_for_the_worker_to_register() -> None:
    from ownvitals.jobs import KINDS

    assert KINDS.get(DISCONNECT_KIND) is None
    registry = KindRegistry()
    register_disconnect_kind(registry)
    register_disconnect_kind(registry)
    kind = registry.get(DISCONNECT_KIND)
    assert kind is not None
    assert kind.input_model is DisconnectInput


def test_the_purge_repeats_the_session_windows() -> None:
    assert STEPUP_WINDOW == SESSIONS_STEPUP
    assert RECENT_AUTH_WINDOW == SESSIONS_RECENT_AUTH
