"""Mandatory TOTP and the second-factor gate (#11, ownvitals ADR 0003 section 2).

A sign-in by password or Google gives a session that is not full: it reaches only the session
answer, sign-out, health, and either the first enrolment (no TOTP yet) or the code check (a
confirmed TOTP). Everything else answers 403 ``two_factor_required``. The first test enumerates
every route the app registers, so a new route can't skip the gate. Every account is synthetic.
"""

import re
from typing import Any
from uuid import UUID

import httpx
import pytest

from authtools import (  # noqa: F401 - fixtures
    ORIGIN,
    TEST_TOTP_SECRET,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    session_cookie,
    smtp_fixture,
)
from flows import Browser, pass_second_factor, reason, signed_in, user_with_password
from mfatools import FakeNotifier, notifier_fixture  # noqa: F401 - fixtures
from oauthtools import fake_fixture, make_invite, oenv_fixture, sign_in_with  # noqa: F401
from oidcfake import FakeProviders
from ownvitals.auth import gate, purge, sessions, totp
from ownvitals.auth.cookies import SESSION_COOKIE

UUID_TEXT = "00000000-0000-0000-0000-000000000001"
PARAM = re.compile(r"{(\w+)(?::\w+)?}")
SAFE = {"GET", "HEAD", "OPTIONS"}


def concrete(path: str) -> str:
    return PARAM.sub(lambda m: "google" if m.group(1) == "provider" else UUID_TEXT, path)


def _flatten(routes: Any) -> list[Any]:
    """Every route object, through FastAPI's included routers (0.16x keeps them as
    ``_IncludedRouter`` entries whose ``effective_candidates`` carry the full paths)."""
    found: list[Any] = []
    for route in routes:
        candidates: Any = getattr(route, "effective_candidates", None)
        if candidates is None:
            found.append(route)
            continue
        items: Any = candidates() if callable(candidates) else candidates
        found.extend(items)
        low: Any = getattr(route, "effective_low_priority_routes", None)
        if low is not None:
            more: Any = low() if callable(low) else low
            found.extend(more)
    return found


def app_routes(env: Env) -> list[tuple[str, str]]:
    """Every (method, path) the app registers: API routes, the OAuth callback, the docs."""
    found: set[tuple[str, str]] = set()
    for route in _flatten(env.app.routes):
        path, methods = getattr(route, "path", None), getattr(route, "methods", None)
        if isinstance(path, str) and methods:
            for method in methods:
                if method != "HEAD":
                    found.add((method, path))
    return sorted(found)


async def csrf_of(env: Env, token: str) -> str:
    answer = await env.client.get("/api/auth/session", headers=session_cookie(token))
    return answer.json()["csrf_token"]


async def call(env: Env, token: str, method: str, path: str) -> httpx.Response:
    headers = session_cookie(token)
    if method not in SAFE:
        headers |= {"Origin": ORIGIN, "X-CSRF-Token": await csrf_of(env, token)}
    env.client.cookies.clear()
    return await env.client.request(method, path, headers=headers, json={})


def refused(response: httpx.Response) -> bool:
    if response.status_code != 403:
        return False
    body: Any = response.json()
    return isinstance(body, dict) and body.get("detail") == {"reason": "two_factor_required"}


@pytest.mark.parametrize("state", ["enrol", "verify"])
async def test_every_route_but_the_allowlist_refuses_a_session_without_its_second_factor(
    env: Env, notifier: FakeNotifier, state: str
) -> None:
    user_id = await env.user(f"{state}@example.com")
    if state == "verify":
        await env.enrol(user_id)
    routes = app_routes(env)
    assert len(routes) > 30  # the enumeration found the app's routes
    allowed = {key for key, need in gate.SECOND_FACTOR_ROUTES.items() if need in ("any", state)}
    # Outside /api and /oauth only read-only pages without data are served (the SPA's files, and
    # outside prod FastAPI's Swagger helper page): the gate leaves those GETs alone.
    pages = {(m, p) for m, p in routes if not p.startswith(("/api/", "/oauth/"))}
    assert pages == {("GET", "/docs/oauth2-redirect")}
    allowed |= pages
    for method, path in routes:
        issued = await env.login(user_id)  # fresh: sign-out deletes its session
        response = await call(env, issued.token, method, concrete(path))
        if (method, path) in allowed:
            assert not refused(response), (method, path, response.text)
        else:
            assert refused(response), (method, path, response.status_code, response.text)
    # An unknown path is gated too (fail closed), and so is a write outside /api.
    issued = await env.login(user_id)
    assert refused(await call(env, issued.token, "GET", "/api/no-such-route"))
    assert refused(await call(env, issued.token, "POST", "/anything"))


async def test_each_allowlisted_route_exists(env: Env) -> None:
    """The allowlist names real routes only, so it can't drift from the app."""
    routes = set(app_routes(env))
    for method, path in gate.SECOND_FACTOR_ROUTES:
        assert (method, path) in routes, (method, path)


async def test_a_full_session_is_not_gated(env: Env) -> None:
    issued = await env.full_login(await env.user())
    response = await env.client.get("/api/auth/sessions", headers=session_cookie(issued.token))
    assert response.status_code == 200


async def test_the_dependency_refuses_on_its_own_when_the_middleware_gate_is_off(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two independent checks: with the middleware's gate switched off, ``require_user`` still
    refuses a session without its second factor (and the jobs API, which uses it)."""
    monkeypatch.setattr(gate, "refusal", lambda *_: None)
    issued = await env.login(await env.user())
    for path in ("/api/auth/sessions", "/api/auth/mfa", f"/api/jobs/{UUID_TEXT}"):
        response = await env.client.get(path, headers=session_cookie(issued.token))
        assert refused(response), path


# --- sign-in paths ----------------------------------------------------------------------------


async def session_answer(browser: Browser) -> dict[str, Any]:
    return (await browser.get("/api/auth/session")).json()


async def test_password_sign_in_without_totp_goes_to_enrolment(env: Env) -> None:
    browser = await signed_in(env, second_factor=False)
    body = await session_answer(browser)
    assert body["authenticated"] is True
    session = body["session"]
    assert (session["mfa"], session["totp_enabled"], session["second_factor"]) == (
        False,
        False,
        "enrol",
    )
    assert reason(await browser.get("/api/auth/sessions")) == "two_factor_required"


async def test_password_sign_in_with_totp_must_verify_a_code(
    env: Env, notifier: FakeNotifier
) -> None:
    user_id = await user_with_password(env, "has.totp@example.com")
    await env.enrol(user_id)
    browser = Browser(env)
    assert (await browser.sign_in("has.totp@example.com")).status_code == 200
    body = await session_answer(browser)
    assert (body["session"]["totp_enabled"], body["session"]["second_factor"]) == (True, "verify")
    assert reason(await browser.get("/api/auth/sessions")) == "two_factor_required"
    # The enrolment is not open to a session of an account that has a TOTP.
    started = await browser.post("/api/auth/mfa/totp/start")
    assert reason(started) == "two_factor_required"
    wrong = await browser.post("/api/auth/mfa/verify", {"code": "000000"})
    assert wrong.status_code == 400
    await pass_second_factor(env, browser, user_id)
    body = await session_answer(browser)
    assert (body["session"]["mfa"], body["session"]["second_factor"]) == (True, None)
    assert (await browser.get("/api/auth/sessions")).status_code == 200
    kinds = await env.scalar("SELECT string_agg(kind, ',' ORDER BY at) FROM auth_events")
    assert kinds == "sign_in,mfa_failed,mfa_verified"


async def test_a_recovery_code_completes_the_sign_in(env: Env, notifier: FakeNotifier) -> None:
    browser = await signed_in(env, second_factor=False)
    start = (await browser.post("/api/auth/mfa/totp/start")).json()
    code = totp.code_for_step(start["secret"], int(totp.clock() // totp.PERIOD))
    confirm = {"pending": start["pending"], "code": code}
    codes = (await browser.post("/api/auth/mfa/totp/confirm", confirm)).json()["recovery_codes"]
    later = Browser(env)
    assert (await later.sign_in("user@example.com")).status_code == 200
    assert (await later.post("/api/auth/mfa/verify", {"code": codes[0]})).status_code == 200
    assert (await session_answer(later))["session"]["mfa"] is True


async def test_the_first_enrolment_makes_the_session_full(env: Env, notifier: FakeNotifier) -> None:
    browser = await signed_in(env, second_factor=False)
    before = browser.cookies[SESSION_COOKIE]
    start = await browser.post("/api/auth/mfa/totp/start")
    assert start.status_code == 200
    body = start.json()
    code = totp.code_for_step(body["secret"], int(totp.clock() // totp.PERIOD))
    done = await browser.post(
        "/api/auth/mfa/totp/confirm", {"pending": body["pending"], "code": code}
    )
    assert done.status_code == 200
    assert browser.cookies[SESSION_COOKIE] != before  # rotated
    assert (await session_answer(browser))["session"]["second_factor"] is None
    assert (await browser.get("/api/auth/sessions")).status_code == 200


async def test_google_sign_in_also_needs_the_second_factor(oenv: Env, fake: FakeProviders) -> None:
    browser = Browser(oenv)
    await sign_in_with(browser, fake, "google", invite=await make_invite(oenv))
    assert browser.signed_in
    body = await session_answer(browser)
    assert (body["session"]["mfa"], body["session"]["second_factor"]) == (False, "enrol")
    assert reason(await browser.get("/api/auth/identities")) == "two_factor_required"
    # The next Google sign-in of an account with a TOTP asks for its code.
    user_id = UUID(str(await oenv.scalar("SELECT id FROM users")))
    await oenv.enrol(user_id)
    again = Browser(oenv)
    await sign_in_with(again, fake, "google")
    assert (await session_answer(again))["session"]["second_factor"] == "verify"


async def test_a_sign_in_never_carries_the_second_factor_over(env: Env) -> None:
    """Even over a still-valid full session of the same user, a new sign-in starts without its
    second factor."""
    browser = await signed_in(env)
    assert (await session_answer(browser))["session"]["mfa"] is True
    assert (await browser.sign_in("user@example.com")).status_code == 200
    assert (await session_answer(browser))["session"]["second_factor"] == "verify"
    assert await env.scalar("SELECT mfa_at IS NULL AND stepup_at IS NULL FROM sessions") is True


# --- a session that is not full is short-lived -----------------------------------------------


async def test_a_session_without_its_second_factor_ends_after_ten_minutes(env: Env) -> None:
    user_id = await env.user()
    issued = await env.login(user_id)
    await env.run("UPDATE sessions SET reauth_at = now() - interval '9 minutes'")
    alive = await env.client.get("/api/auth/session", headers=session_cookie(issued.token))
    assert alive.json()["authenticated"] is True
    await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")
    gone = await env.client.get("/api/auth/session", headers=session_cookie(issued.token))
    assert gone.json()["authenticated"] is False
    assert any(
        cookie.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in cookie
        for cookie in gone.headers.get_list("set-cookie")
    )
    assert await env.scalar("SELECT count(*) FROM sessions") == 0


async def test_a_full_session_is_not_ended_by_the_ten_minute_rule(env: Env) -> None:
    issued = await env.full_login(await env.user())
    await env.run("UPDATE sessions SET reauth_at = now() - interval '2 hours'")
    answer = await env.client.get("/api/auth/sessions", headers=session_cookie(issued.token))
    assert answer.status_code == 200


async def test_the_purge_removes_stale_sessions_without_a_second_factor(env: Env) -> None:
    assert purge.PENDING_WINDOW == sessions.PENDING_WINDOW
    user_id = await env.user()
    stale = await env.login(user_id)
    fresh = await env.login(user_id)
    full = await env.full_login(user_id)
    await env.run(
        "UPDATE sessions SET reauth_at = now() - interval '11 minutes' WHERE id IN (:a, :b)",
        a=stale.id,
        b=full.id,
    )
    removed = await purge.purge_expired(env.factory, env.settings)
    assert removed["sessions"] == 1
    left = await env.scalar("SELECT string_agg(id::text, ',' ORDER BY id) FROM sessions")
    assert set(str(left).split(",")) == {str(fresh.id), str(full.id)}


# --- removing the TOTP returns the account to enrolment ----------------------------------------


async def test_removing_the_totp_sends_this_session_to_enrolment_and_signs_out_the_rest(
    env: Env, notifier: FakeNotifier
) -> None:
    browser = await signed_in(env)  # its code counts as the step-up
    user_id = UUID(str(await env.scalar("SELECT id FROM users")))
    other = await env.full_login(user_id)
    before = browser.cookies[SESSION_COOKIE]
    response = await browser.request("DELETE", "/api/auth/mfa/totp")
    assert response.status_code == 204
    assert browser.cookies[SESSION_COOKIE] != before  # rotated
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0
    assert await env.scalar("SELECT count(*) FROM sessions WHERE id = :i", i=other.id) == 0
    body = await session_answer(browser)
    assert (body["session"]["mfa"], body["session"]["second_factor"]) == (False, "enrol")
    assert reason(await browser.get("/api/auth/sessions")) == "two_factor_required"
    # It can enrol a new authenticator straight away.
    assert (await browser.post("/api/auth/mfa/totp/start")).status_code == 200


async def test_the_session_answer_says_what_is_missing(env: Env) -> None:
    """The contract the SPA reads (frontend ``api/auth.ts``): ``totp_enabled`` and
    ``second_factor`` sit in the session answer, so no other call is needed to route."""
    user_id = await env.user()
    issued = await env.login(user_id)
    body = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()
    assert set(body["session"]) == {
        "id",
        "expires_at",
        "recent_auth",
        "mfa",
        "stepup",
        "totp_enabled",
        "second_factor",
        "admin_pending_mfa",
    }
    assert body["session"]["second_factor"] == "enrol"
    assert await env.enrol(user_id) == TEST_TOTP_SECRET
    body = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()
    assert (body["session"]["totp_enabled"], body["session"]["second_factor"]) == (True, "verify")
    # A second factor passed for an authenticator that is gone no longer counts.
    await env.run("UPDATE sessions SET mfa_at = now()")
    await env.run("DELETE FROM mfa_totp")
    body = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()
    assert (body["session"]["mfa"], body["session"]["second_factor"]) == (False, "enrol")
