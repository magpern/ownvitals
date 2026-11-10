"""The Withings connect routes (#296): start, the callback and the first import job. Synthetic only.

The code exchange runs through the real egress entry point with the in-process fake as its
transport; no socket is opened and no real Withings account exists. Every account, token and
Withings user below is synthetic.
"""

import json
import logging
import urllib.request
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from flows import Browser, age_reauth, reason, signed_in
from ownvitals.egress import core
from ownvitals.egress import exchange_withings_code as egress_exchange
from ownvitals.withings import oauth as oauth_module
from ownvitals.withings.apps import create_app
from ownvitals.withings.oauth_state import StateStore
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingswire import ACCOUNT_HOST, AUTHORIZE_PATH, Request

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")
CLIENT = "synthetic-connect-client"
SECRET = "synthetic-connect-secret"
BASE = "https://app.example.test"
REDIRECT = BASE + "/api/oauth/withings/callback"
START = "/api/connections/withings/start"
EXCHANGE = "v2/oauth2:requesttoken"


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


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeWithings:
    """The fake Withings, wired in as the transport of web's one code exchange."""
    world = FakeWithings(seed=296)
    world.add_app(CLIENT, SECRET, (REDIRECT,))
    world.add_user()
    _exchange_through(monkeypatch, FakeWithingsTransport(world))
    return world


def _exchange_through(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    real = egress_exchange

    async def through(**kwargs: Any) -> httpx.Response:
        return await real(**{**kwargs, "inner": transport})

    monkeypatch.setattr(oauth_module, "exchange_withings_code", through)


async def _app(env: Env, owner: object) -> int:
    return await create_app(owner, env.factory, env.app.state.keyring, CLIENT, BASE, SECRET)  # type: ignore[arg-type]


def _scalar(url: str, sql: str, **params: object) -> object:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


def _exchanges(fake: FakeWithings) -> int:
    return sum(1 for entry in fake.log if entry.endpoint == EXCHANGE)


async def _start(browser: Browser) -> str:
    started = await browser.post(START, {})
    assert started.status_code == 200, started.text
    return str(started.json()["authorize_url"])


def _consent(fake: FakeWithings, authorize_url: str) -> str:
    """The user approves on the fake's authorize page: the callback path and query."""
    page = fake.handle(Request("GET", authorize_url))
    assert page.status_code == 302
    back = urlsplit(page.headers["location"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    return f"{back.path}?{back.query}"


def _outcome(response: httpx.Response) -> str:
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert (target.scheme, target.netloc, target.path) == ("", "", "/onboarding")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert set(parse_qs(target.query)) == {"withings"}
    return parse_qs(target.query)["withings"][0]


def _query(callback: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(callback).query).items()}


# ---------------------------------------------------------------------------------- start


async def test_start_answers_the_authorize_url_with_a_state(env: Env, owner: object) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    started = await browser.post(START, {})
    assert started.status_code == 200
    assert started.headers["cache-control"] == "no-store"
    assert set(started.json()) == {"authorize_url"}
    url = urlsplit(started.json()["authorize_url"])
    assert (url.scheme, url.netloc, url.path) == ("https", ACCOUNT_HOST, AUTHORIZE_PATH)
    query = parse_qs(url.query)
    assert set(query) == {"response_type", "client_id", "redirect_uri", "scope", "state"}
    assert (query["client_id"], query["redirect_uri"]) == ([CLIENT], [REDIRECT])
    assert len(query["state"][0]) >= 32
    assert isinstance(env.app.state.withings_oauth_states, StateStore)


async def test_start_needs_a_step_up_and_recent_authentication(env: Env, owner: object) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    refused = await browser.post(START, {})
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")
    await age_reauth(env)
    refused = await browser.post(START, {})
    assert (refused.status_code, reason(refused)) == (403, "reauth_required")


async def test_start_refuses_anonymous_and_without_csrf(env: Env, owner: object) -> None:
    await _app(env, owner)
    anonymous = await Browser(env).post(START, {})
    assert anonymous.status_code == 401
    browser = await signed_in(env)
    forged = await browser.post(START, {}, csrf="not-the-token")
    assert forged.status_code == 403


async def test_start_without_a_configured_app_is_503(env: Env, database_url: str) -> None:
    engine = create_engine(database_url)  # the apps of earlier tests (clean_auth keeps them)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM withings_apps"))
    finally:
        engine.dispose()
    browser = await signed_in(env)
    refused = await browser.post(START, {})
    assert (refused.status_code, reason(refused)) == (503, "withings_not_configured")


async def test_start_uses_the_newest_app(env: Env, owner: object) -> None:
    await create_app(owner, env.factory, env.app.state.keyring, "synthetic-old", BASE, SECRET)  # type: ignore[arg-type]
    await _app(env, owner)
    browser = await signed_in(env)
    assert parse_qs(urlsplit(await _start(browser)).query)["client_id"] == [CLIENT]


# ------------------------------------------------------------------------------- callback


async def test_the_callback_connects_and_queues_one_import(
    env: Env, owner: object, fake: FakeWithings, database_url: str
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    callback = _consent(fake, await _start(browser))
    assert _outcome(await browser.get(callback)) == "connected"
    assert _exchanges(fake) == 1
    listed = (await browser.get("/api/connections")).json()["connections"]
    assert [(c["provider"], c["status"]) for c in listed] == [("withings", "connected")]
    jobs = _scalar(
        database_url,
        "SELECT json_agg(json_build_object('input', input::text, 'lane', lane, 'state', state)) "
        "FROM jobs WHERE kind = 'withings_connect'",
    )
    assert isinstance(jobs, list)
    assert len(jobs) == 1
    assert json.loads(jobs[0]["input"]) == {"connection_id": listed[0]["id"]}
    assert (jobs[0]["lane"], jobs[0]["state"]) == (1, "queued")


async def test_a_replayed_callback_is_refused_without_a_second_exchange(
    env: Env, owner: object, fake: FakeWithings
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    callback = _consent(fake, await _start(browser))
    assert _outcome(await browser.get(callback)) == "connected"
    assert _outcome(await browser.get(callback)) == "state_rejected"
    assert _exchanges(fake) == 1


async def test_a_reconnect_does_not_queue_a_second_import_while_one_waits(
    env: Env, owner: object, fake: FakeWithings, database_url: str
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    for _ in range(2):
        assert _outcome(await browser.get(_consent(fake, await _start(browser)))) == "connected"
    count = "SELECT count(*) FROM jobs WHERE kind = 'withings_connect'"
    assert _scalar(database_url, count) == 1


async def test_the_state_is_bound_to_the_session_that_started(
    env: Env, owner: object, fake: FakeWithings
) -> None:
    await _app(env, owner)
    starter = await signed_in(env, "starter@example.com")
    callback = _consent(fake, await _start(starter))
    other = await signed_in(env, "other@example.com")
    assert _outcome(await other.get(callback)) == "state_rejected"
    assert _outcome(await Browser(env).get(callback)) == "state_rejected"
    assert _exchanges(fake) == 0
    # The foreign try took the state before comparing the session, so it is spent for the
    # starter too: a state that reached another browser is never usable again.
    assert _outcome(await starter.get(callback)) == "state_rejected"


async def test_a_denial_at_withings_spends_the_state(
    env: Env, owner: object, fake: FakeWithings
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    callback = _consent(fake, await _start(browser))
    state = _query(callback)["state"]
    denied = await browser.get(f"/api/oauth/withings/callback?error=access_denied&state={state}")
    assert _outcome(denied) == "denied"
    assert _outcome(await browser.get(callback)) == "state_rejected"
    assert _exchanges(fake) == 0


@pytest.mark.parametrize(
    "query",
    [
        "",
        "code=synthetic-code",
        "state=unknown-synthetic-state&code=synthetic-code",
        "state=a&state=b&code=synthetic-code",
        "state=" + "x" * 600 + "&code=synthetic-code",
    ],
)
async def test_a_missing_or_unknown_state_is_rejected_without_an_exchange(
    env: Env, owner: object, fake: FakeWithings, query: str
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    await _start(browser)
    answer = await browser.get(f"/api/oauth/withings/callback?{query}")
    assert _outcome(answer) == "state_rejected"
    assert _exchanges(fake) == 0


async def test_a_missing_code_spends_the_state(env: Env, owner: object, fake: FakeWithings) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    callback = _consent(fake, await _start(browser))
    state = _query(callback)["state"]
    assert _outcome(await browser.get(f"/api/oauth/withings/callback?state={state}")) == "error"
    assert _outcome(await browser.get(callback)) == "state_rejected"


async def test_a_refused_exchange_stores_nothing(
    env: Env, owner: object, fake: FakeWithings, database_url: str
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    callback = _consent(fake, await _start(browser))
    fake.inject("status", at=1, endpoint=EXCHANGE, status=503)
    assert _outcome(await browser.get(callback)) == "exchange_failed"
    assert (await browser.get("/api/connections")).json()["connections"] == []
    assert _scalar(database_url, "SELECT count(*) FROM jobs") == 0


async def test_a_transport_failure_of_the_exchange_is_exchange_failed(
    env: Env, owner: object, fake: FakeWithings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    callback = _consent(fake, await _start(browser))

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic outage", request=request)

    _exchange_through(monkeypatch, httpx.MockTransport(broken))
    assert _outcome(await browser.get(callback)) == "exchange_failed"
    assert (await browser.get("/api/connections")).json()["connections"] == []


async def test_a_lapsed_step_up_at_the_callback_is_refused(
    env: Env, owner: object, fake: FakeWithings
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    callback = _consent(fake, await _start(browser))
    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    assert _outcome(await browser.get(callback)) == "step_up_required"
    assert _exchanges(fake) == 0


async def test_a_withings_account_linked_elsewhere_is_refused(
    env: Env, owner: object, fake: FakeWithings
) -> None:
    await _app(env, owner)
    first = await signed_in(env, "first@example.com")
    assert _outcome(await first.get(_consent(fake, await _start(first)))) == "connected"
    second = await signed_in(env, "second@example.com")
    assert _outcome(await second.get(_consent(fake, await _start(second)))) == "linked_elsewhere"
    assert (await second.get("/api/connections")).json()["connections"] == []


async def test_neither_the_code_nor_the_state_is_logged_or_echoed(
    env: Env, owner: object, fake: FakeWithings, caplog: pytest.LogCaptureFixture
) -> None:
    await _app(env, owner)
    browser = await signed_in(env)
    caplog.set_level(logging.DEBUG)
    callback = _consent(fake, await _start(browser))
    values = _query(callback)
    answer = await browser.get(callback)
    replay = await browser.get(callback)
    for response in (answer, replay):
        assert values["code"] not in response.headers["location"]
        assert values["state"] not in response.headers["location"]
        assert response.content == b""
    assert values["code"] not in caplog.text
    assert values["state"] not in caplog.text


async def test_start_counts_against_the_per_ip_oauth_start_limit(
    env: Env, owner: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0004 section 7: the start is limited per IP as well as per user. A two-slot window
    here (the real one is 30 per 15 minutes, shared with Google's start); the per-user state
    limit (30) is far away, so the third start is refused by the IP limit alone."""
    from ownvitals.auth import deps
    from ownvitals.auth.ratelimit import OAUTH_IP, Limit

    monkeypatch.setattr(deps, "OAUTH_IP", Limit(OAUTH_IP.action, OAUTH_IP.scope, 2, 900))
    await _app(env, owner)
    browser = await signed_in(env)
    for _ in range(2):
        assert (await browser.post(START, {})).status_code == 200
    refused = await browser.post(START, {})
    assert refused.status_code == 429
    assert "retry-after" in refused.headers
    assert "authorize_url" not in refused.text


async def test_the_per_ip_limit_refuses_before_a_state_is_issued(
    env: Env, owner: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IP limit runs before ``withings.oauth.start``: a refused start leaves no state behind
    and spends none of the user's 30 starts (#308)."""
    from ownvitals.auth import deps
    from ownvitals.auth.ratelimit import OAUTH_IP, Limit

    issued: list[str | None] = []
    real_issue = StateStore.issue

    async def counting(self: StateStore, **kwargs: Any) -> str | None:
        state = await real_issue(self, **kwargs)
        issued.append(state)
        return state

    monkeypatch.setattr(StateStore, "issue", counting)
    monkeypatch.setattr(deps, "OAUTH_IP", Limit(OAUTH_IP.action, OAUTH_IP.scope, 1, 900))
    await _app(env, owner)
    browser = await signed_in(env)
    assert (await browser.post(START, {})).status_code == 200
    assert len(issued) == 1
    for _ in range(2):
        assert (await browser.post(START, {})).status_code == 429
    assert len(issued) == 1


async def test_a_callback_in_a_session_without_its_second_factor_is_rejected(
    env: Env, owner: object, fake: FakeWithings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session that started, its step-up still recent, no longer full. The middleware's gate
    answers 403 first; with the gate switched off, the route's own check (no ``UserContext``)
    still answers ``state_rejected`` before the state is taken or a code exchanged: two
    independent checks (ADR 0003 section 2, #308)."""
    from ownvitals.auth import gate

    await _app(env, owner)
    browser = await signed_in(env)
    callback = _consent(fake, await _start(browser))
    await env.run("UPDATE sessions SET mfa_at = NULL")
    gated = await browser.get(callback)
    assert (gated.status_code, reason(gated)) == (403, "two_factor_required")
    monkeypatch.setattr(gate, "refusal", lambda *_: None)
    assert _outcome(await browser.get(callback)) == "state_rejected"
    assert _exchanges(fake) == 0
