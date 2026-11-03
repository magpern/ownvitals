"""The disconnect and account-delete routes on the revoke-first path (#163). Synthetic only.

The routes enqueue a ``withings_disconnect`` job and wait for it; the revoke runs in the job
(here an in-process worker over the fake), never in the route. Every token, session and
Withings account below is synthetic.
"""

import asyncio
import contextlib
import json
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
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
from ownvitals.connections.repository import disconnect
from ownvitals.context import UserContext
from ownvitals.egress import core, install_worker_allowlist
from ownvitals.jobs import KindRegistry, queue, service
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.withings.disconnect import (
    DISCONNECT_KIND,
    REMOVAL,
    DisconnectInput,
    disconnect_withings,
)
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
        # Runs once, just before the OAuth revoke is answered (a race the test stages).
        self.before_grant_revoke: Callable[[], Awaitable[None]] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = parse_qs((await request.aread()).decode())
        action = body.get("action", [""])[0]
        if action == "revoke" and self.connection_id is not None:
            held = _scalar(self.database_url, _CREDENTIALS, c=self.connection_id) == 1
            self.revokes.append((request.url.path.strip("/"), held))
        hook = self.before_grant_revoke
        if action == "revoke" and request.url.path.startswith("/v2/oauth2") and hook is not None:
            self.before_grant_revoke = None
            await hook()
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


# --- review of 9295c57: a full queue never blocks; a late run leaves a reconnect alone ----------


def _system_jobs(url: str, owner_id: UUID, count: int) -> None:
    """Queued webhook jobs the owner did not create, over ``jobs_per_user`` (2)."""
    for _ in range(count):
        _run(
            url,
            "INSERT INTO jobs (owner_id, kind, origin, state, input) "
            "VALUES (:o, 'withings_sync', 'webhook', 'queued', '{}')",
            o=owner_id,
        )


def _queued_disconnect(url: str, owner_id: UUID) -> None:
    """An earlier ``withings_disconnect`` still queued: the owner's slot is taken."""
    _run(
        url,
        "INSERT INTO jobs (owner_id, kind, origin, state, input, idempotency_key, request_hash) "
        "VALUES (:o, :k, 'user', 'queued', '{}', 'synthetic-disconnect-0001', :h)",
        o=owner_id,
        k=DISCONNECT_KIND,
        h=b"\x22" * 32,
    )


def _done(response: httpx.Response, path: str) -> dict[str, object]:
    assert response.status_code == 200, (path, response.text)
    return response.json()


@pytest.mark.parametrize("path", ["/api/connections/withings", "/api/auth/account"])
async def test_a_queue_full_of_system_jobs_does_not_block_the_revoke(
    env: Env, owner: object, database_url: str, path: str
) -> None:
    """The disconnect job takes a slot of its own: five webhook jobs (``jobs_per_user`` is 2)
    neither refuse the request nor keep the worker's revoke from running."""
    linked = await _linked(env, owner, database_url)
    _system_jobs(database_url, linked.ctx.user_id, 5)
    async with worker(env, linked):
        body = _done(await linked.browser.request("DELETE", path), path)
    assert body["revoke_failed"] is False
    assert linked.spy.revokes == [("notify", True), ("notify", True), ("v2/oauth2", True)]
    if path == "/api/auth/account":
        assert _scalar(database_url, "SELECT count(*) FROM users") == 0
    else:
        assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 0


@pytest.mark.parametrize("path", ["/api/connections/withings", "/api/auth/account"])
async def test_when_no_job_can_be_queued_the_route_clears_locally(
    env: Env, owner: object, database_url: str, path: str
) -> None:
    """The owner's disconnect slot is taken and the queue is full of system jobs: no 429; the
    credentials are cleared (or the account deleted) and the revoke is reported failed."""
    linked = await _linked(env, owner, database_url)
    _system_jobs(database_url, linked.ctx.user_id, 5)
    _queued_disconnect(database_url, linked.ctx.user_id)
    body = _done(await linked.browser.request("DELETE", path), path)
    assert (body["revoke_failed"], body["removal"]) == (True, REMOVAL)
    assert linked.fake.log == []
    if path == "/api/auth/account":
        assert _scalar(database_url, "SELECT count(*) FROM users") == 0
    else:
        assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 0
        assert _job_states(database_url, linked.ctx.user_id) == [("queued", None)]


@pytest.mark.parametrize("path", ["/api/connections/withings", "/api/auth/account"])
async def test_a_failed_enqueue_falls_through_to_the_local_clear(
    env: Env, owner: object, database_url: str, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    linked = await _linked(env, owner, database_url)

    async def refused(*_: object, **__: object) -> object:
        raise RuntimeError("synthetic enqueue failure")

    monkeypatch.setattr(connection_routes.service, "create", refused)
    body = _done(await linked.browser.request("DELETE", path), path)
    assert (body["revoke_failed"], body["removal"]) == (True, REMOVAL)
    assert linked.fake.log == []
    if path == "/api/auth/account":
        assert _scalar(database_url, "SELECT count(*) FROM users") == 0
    else:
        assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 0


async def test_a_late_run_leaves_a_reconnected_connection_alone(
    env: Env, owner: object, database_url: str
) -> None:
    """The route gave up and cleared the row; the user linked the same Withings account again
    (the same row, a new generation); then the old run reaches its clear. It must not drop the
    new grant."""
    linked = await _linked(env, owner, database_url)
    install_worker_allowlist(ai_enabled=False)
    ring = env.app.state.keyring

    async def route_gave_up_then_reconnect() -> None:
        await disconnect(
            env.factory,
            linked.ctx,
            "withings",
            revoke_failed=True,
            connection_id=linked.connection_id,
        )
        await store_grant(
            env.factory,
            ring,
            linked.ctx,
            linked.app_id,
            linked.userid,
            "user.metrics",
            "synthetic-access-token-relinked",
            "synthetic-refresh-token-relinked",
            expires_in=10800,
        )

    linked.spy.before_grant_revoke = route_gave_up_then_reconnect
    result = await disconnect_withings(
        env.factory, ring, linked.ctx, linked.connection_id, stepped_up=True, inner=linked.spy
    )
    assert result.cleared is False
    assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 1
    status = "SELECT status, last_error FROM connections WHERE id = :c"
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(text(status), {"c": linked.connection_id}).one()
    finally:
        engine.dispose()
    assert (row.status, row.last_error) == ("connected", None)


# --- end to end through the real worker loop and its registry -----------------------------------


async def test_the_worker_loop_runs_the_registered_disconnect(
    env: Env, owner: object, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route, job, ``Worker.run`` over ``worker_registry()`` (the production registration),
    revokes against the fake, credentials cleared, no revoke failure. Only the transport (the
    fake instead of the egress client) and the key ring (the app's) are substituted."""
    from ownvitals.auth import purge as purge_module
    from ownvitals.worker.kinds import worker_registry
    from ownvitals.worker.loop import Worker, create_worker_engine

    linked = await _linked(env, owner, database_url)
    install_worker_allowlist(ai_enabled=False)
    real = purge_module.disconnect_withings

    async def over_the_fake(*args: Any, **kwargs: Any) -> Any:
        return await real(*args, **{**kwargs, "inner": linked.spy, "sleep": _asleep})

    async def app_ring(_: object) -> object:
        return env.app.state.keyring

    monkeypatch.setattr(purge_module, "disconnect_withings", over_the_fake)
    monkeypatch.setattr(purge_module, "_worker_ring", app_ring)
    registry = worker_registry()
    assert registry.get(DISCONNECT_KIND) is not None
    settings = env.settings.model_copy(update={"worker_poll_seconds": 0.1})
    engine = create_worker_engine(settings)
    loop = Worker(
        settings, engine, registry=registry, accounts=ACCOUNTS, keyring=env.app.state.keyring
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(loop.run(stop))
    try:
        response = await linked.browser.request("DELETE", "/api/connections/withings")
    finally:
        stop.set()
        await asyncio.wait_for(task, 40)
        await engine.dispose()
    assert response.status_code == 200, response.text
    assert response.json()["revoke_failed"] is False
    assert response.json()["removal"] is None
    assert linked.spy.revokes == [("notify", True), ("notify", True), ("v2/oauth2", True)]
    assert _scalar(database_url, _CREDENTIALS, c=linked.connection_id) == 0
    assert _job_states(database_url, linked.ctx.user_id) == [("completed", None)]
