"""The second-factor gate after the owner's decision of 2026-11-12 (#475; roadmap Amendment 13,
ownvitals ADR 0003 amendment of 2026-11-12). It replaces #11's ``test_auth_mandatory_totp.py``.

Three kinds of account, three rules:

- **with a confirmed TOTP:** unchanged. A sign-in gives a session that owes its code
  (``verify``): it reaches only the session answer, sign-out, health and the code check, and
  everything else answers 403 ``two_factor_required``;
- **an admin** (stored role ``admin``, or the address in ``OWNVITALS_ADMIN_EMAILS``) **without a
  TOTP:** must enrol (``enrol``) before anything else, and the admin area keeps its 12-hour rule;
- **a user without a TOTP:** full after the first factor; no route answers
  ``two_factor_required``.

The first test enumerates every route the app registers for each kind, so a new route can't
skip the gate. Every account is synthetic.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from authtools import (  # noqa: F401 - fixtures
    ADMIN_EMAIL,
    ORIGIN,
    TEST_TOTP_SECRET,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    running_env,
    session_cookie,
    smtp_fixture,
)
from flows import (
    PASSWORD,
    Browser,
    pass_second_factor,
    reason,
    signed_in,
    user_with_password,
)
from mfatools import FakeNotifier, notifier_fixture  # noqa: F401 - fixtures
from oauthtools import fake_fixture, make_invite, oenv_fixture, sign_in_with  # noqa: F401
from oidcfake import FakeProviders
from ownvitals.auth import gate, purge, second_factor_policy, sessions, totp
from ownvitals.auth.cookies import SESSION_COOKIE

UUID_TEXT = "00000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
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


def info_of(
    *, totp_enabled: bool, required: bool, mfa: bool, role: str = "user"
) -> sessions.SessionInfo:
    """A resolved session as of ``NOW`` (pure: no database)."""
    return sessions.SessionInfo(
        id=UUID(UUID_TEXT),
        user_id=UUID(UUID_TEXT),
        email="user@example.com",
        stored_role=role,  # pyright: ignore[reportArgumentType]
        created_at=NOW,
        last_seen_at=NOW,
        expires_at=NOW + timedelta(days=30),
        reauth_at=NOW,
        mfa_at=NOW if mfa else None,
        stepup_at=None,
        now=NOW,
        totp_enabled=totp_enabled,
        totp_required=required,
    )


# --- the rule, pure ----------------------------------------------------------------------------


def test_who_must_have_a_totp() -> None:
    listed = frozenset({"owner@example.com"})
    assert second_factor_policy.totp_required("admin", "a@example.com", listed) is True
    assert second_factor_policy.totp_required("user", "owner@example.com", listed) is True
    assert second_factor_policy.totp_required("user", "a@example.com", listed) is False
    assert second_factor_policy.totp_required("user", "a@example.com", frozenset()) is False


def test_the_first_factor_steps_up_only_without_a_totp_and_when_not_required() -> None:
    steps_up = second_factor_policy.first_factor_steps_up
    assert steps_up(totp_enabled=False, required=False) is True
    assert steps_up(totp_enabled=True, required=False) is False  # never weakened by #475
    assert steps_up(totp_enabled=False, required=True) is False  # an admin enrols instead
    assert steps_up(totp_enabled=True, required=True) is False


@pytest.mark.parametrize(
    ("totp_enabled", "required", "mfa", "full", "owes"),
    [
        (True, False, False, False, "verify"),  # a TOTP account: unchanged
        (True, False, True, True, None),
        (True, True, False, False, "verify"),  # an admin with a TOTP: unchanged
        (True, True, True, True, None),
        (False, True, False, False, "enrol"),  # an admin without: must enrol
        (False, True, True, False, "enrol"),  # an mfa_at whose TOTP is gone does not count
        (False, False, False, True, None),  # a user without: full after the first factor
        (False, False, True, True, None),
    ],
)
def test_when_a_session_is_full(
    totp_enabled: bool, required: bool, mfa: bool, full: bool, owes: str | None
) -> None:
    info = info_of(totp_enabled=totp_enabled, required=required, mfa=mfa)
    assert (info.full, info.second_factor) == (full, owes)


def test_the_admin_role_still_needs_a_totp_passed_in_this_session() -> None:
    def role(totp_enabled: bool, mfa: bool) -> str:
        info = info_of(totp_enabled=totp_enabled, required=True, mfa=mfa, role="admin")
        return info.role

    assert role(True, True) == "admin"
    assert role(True, False) == "user"
    assert role(False, True) == "user"  # an mfa_at whose TOTP is gone never makes an admin
    assert info_of(totp_enabled=False, required=False, mfa=False).may_approve_qr is False


def test_the_session_class_fails_closed_when_the_requirement_is_not_given() -> None:
    info = info_of(totp_enabled=False, required=False, mfa=False)
    default = sessions.SessionInfo(
        **{k: v for k, v in _fields(info).items() if k != "totp_required"}
    )
    assert default.totp_required is True
    assert default.full is False


def _fields(info: sessions.SessionInfo) -> dict[str, Any]:
    return {name: getattr(info, name) for name in info.__dataclass_fields__}


# --- every route, for each kind of account -------------------------------------------------


@pytest.mark.parametrize("state", ["enrol", "verify"])
async def test_every_route_but_the_allowlist_refuses_a_session_that_owes_its_second_factor(
    env: Env, notifier: FakeNotifier, state: str
) -> None:
    """``verify``: an account with a TOTP (unchanged); ``enrol``: an admin without one."""
    if state == "verify":
        user_id = await env.user("verify@example.com")
        await env.enrol(user_id)
    else:
        user_id = await env.user("admin@example.com", role="admin")
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


async def test_the_gate_lets_every_route_through_for_a_user_without_a_totp(env: Env) -> None:
    """The middleware's decision for every registered route (the handlers are not run: some
    stream, some change the account). The dependency side is the next test."""
    info = info_of(totp_enabled=False, required=False, mfa=False)
    for method, path in [*app_routes(env), ("GET", "/api/no-such-route"), ("POST", "/x")]:
        assert gate.refusal(method, concrete(path), info) is None, (method, path)
    for owing in (
        info_of(totp_enabled=True, required=False, mfa=False),
        info_of(totp_enabled=False, required=True, mfa=False),
    ):
        assert gate.refusal("GET", "/api/auth/sessions", owing) == gate.REASON


async def test_a_user_without_a_totp_gets_a_context_after_the_password(env: Env) -> None:
    issued = await env.login(await env.user())
    for path in ("/api/auth/sessions", "/api/auth/mfa", "/api/auth/activity"):
        response = await env.client.get(path, headers=session_cookie(issued.token))
        assert response.status_code == 200, (path, response.text)


async def test_each_allowlisted_route_exists(env: Env) -> None:
    """The allowlist names real routes only, so it can't drift from the app."""
    routes = set(app_routes(env))
    for method, path in gate.SECOND_FACTOR_ROUTES:
        assert (method, path) in routes, (method, path)


async def test_the_dependency_refuses_on_its_own_when_the_middleware_gate_is_off(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two independent checks: with the middleware's gate switched off, ``require_user`` still
    refuses a session that owes its second factor (and the jobs API, which uses it)."""
    monkeypatch.setattr(gate, "refusal", lambda *_: None)
    with_totp = await env.user("with.totp@example.com")
    await env.enrol(with_totp)
    admin = await env.user("admin@example.com", role="admin")
    for user_id in (with_totp, admin):
        issued = await env.login(user_id)
        for path in ("/api/auth/sessions", "/api/auth/mfa", f"/api/jobs/{UUID_TEXT}"):
            response = await env.client.get(path, headers=session_cookie(issued.token))
            assert refused(response), path


# --- sign-in paths ----------------------------------------------------------------------------


async def session_answer(browser: Browser) -> dict[str, Any]:
    return (await browser.get("/api/auth/session")).json()


def owed(body: dict[str, Any]) -> tuple[Any, ...]:
    session = body["session"]
    return (
        session["mfa"],
        session["totp_enabled"],
        session["totp_required"],
        session["second_factor"],
    )


async def test_password_sign_in_without_totp_reaches_the_app(env: Env) -> None:
    browser = await signed_in(env, second_factor=False)
    body = await session_answer(browser)
    assert body["authenticated"] is True
    assert owed(body) == (True, False, False, None)
    assert (await browser.get("/api/auth/sessions")).status_code == 200
    # A full session without TOTP is not ended by the ten-minute rule, nor purged.
    await env.run("UPDATE sessions SET reauth_at = now() - interval '2 hours'")
    assert (await browser.get("/api/auth/sessions")).status_code == 200
    assert (await purge.purge_expired(env.factory, env.settings))["sessions"] == 0
    assert await env.scalar("SELECT count(*) FROM sessions") == 1


async def test_password_sign_in_with_totp_must_verify_a_code(
    env: Env, notifier: FakeNotifier
) -> None:
    """Unchanged by #475."""
    user_id = await user_with_password(env, "has.totp@example.com")
    await env.enrol(user_id)
    browser = Browser(env)
    assert (await browser.sign_in("has.totp@example.com")).status_code == 200
    body = await session_answer(browser)
    assert owed(body) == (False, True, False, "verify")
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


async def enrol_through_the_api(browser: Browser) -> list[str]:
    start = await browser.post("/api/auth/mfa/totp/start")
    assert start.status_code == 200, start.text
    body = start.json()
    code = totp.code_for_step(body["secret"], int(totp.clock() // totp.PERIOD))
    done = await browser.post(
        "/api/auth/mfa/totp/confirm", {"pending": body["pending"], "code": code}
    )
    assert done.status_code == 200, done.text
    return done.json()["recovery_codes"]


async def test_a_user_turns_totp_on_later_and_every_other_session_then_owes_the_code(
    env: Env, notifier: FakeNotifier
) -> None:
    browser = await signed_in(env, second_factor=False)
    user_id = UUID(str(await env.scalar("SELECT id FROM users")))
    other = await env.login(user_id)  # full: the account has no TOTP yet
    first = await env.client.get("/api/auth/sessions", headers=session_cookie(other.token))
    assert first.status_code == 200
    before = browser.cookies[SESSION_COOKIE]
    codes = await enrol_through_the_api(browser)
    assert browser.cookies[SESSION_COOKIE] != before  # rotated
    assert owed(await session_answer(browser)) == (True, True, False, None)
    assert notifier.enrolled == [user_id]
    # The other session passed no code: it now owes one, exactly as a TOTP account's sign-in.
    answer = await env.client.get("/api/auth/sessions", headers=session_cookie(other.token))
    assert refused(answer)
    # The next sign-in asks for the code, and a recovery code completes it.
    later = Browser(env)
    assert (await later.sign_in("user@example.com")).status_code == 200
    assert (await session_answer(later))["session"]["second_factor"] == "verify"
    assert (await later.post("/api/auth/mfa/verify", {"code": codes[0]})).status_code == 200
    assert (await session_answer(later))["session"]["mfa"] is True


async def test_turning_totp_on_later_needs_a_recent_first_factor(
    env: Env, notifier: FakeNotifier
) -> None:
    browser = await signed_in(env, second_factor=False)
    await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")
    assert reason(await browser.post("/api/auth/mfa/totp/start")) == "reauth_required"
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    assert (await browser.post("/api/auth/mfa/totp/start")).status_code == 200


async def test_google_sign_in_without_totp_reaches_the_app(oenv: Env, fake: FakeProviders) -> None:
    browser = Browser(oenv)
    await sign_in_with(browser, fake, "google", invite=await make_invite(oenv))
    assert browser.signed_in
    assert owed(await session_answer(browser)) == (True, False, False, None)
    assert (await browser.get("/api/auth/identities")).status_code == 200
    # The next Google sign-in of an account with a TOTP asks for its code (unchanged).
    user_id = UUID(str(await oenv.scalar("SELECT id FROM users")))
    await oenv.enrol(user_id)
    again = Browser(oenv)
    await sign_in_with(again, fake, "google")
    assert (await session_answer(again))["session"]["second_factor"] == "verify"
    assert reason(await again.get("/api/auth/identities")) == "two_factor_required"


async def test_a_sign_in_never_carries_the_second_factor_over(env: Env) -> None:
    """Even over a still-valid full session of the same user, a new sign-in of an account with
    a TOTP starts without its second factor."""
    browser = await signed_in(env)
    assert (await session_answer(browser))["session"]["mfa"] is True
    assert (await browser.sign_in("user@example.com")).status_code == 200
    assert (await session_answer(browser))["session"]["second_factor"] == "verify"
    assert await env.scalar("SELECT mfa_at IS NULL AND stepup_at IS NULL FROM sessions") is True


# --- admins must have a TOTP -------------------------------------------------------------------


async def test_an_admin_without_totp_must_enrol_before_anything_else(
    env: Env, notifier: FakeNotifier
) -> None:
    user_id = await user_with_password(env, "admin@example.com", role="admin")
    browser = Browser(env)
    assert (await browser.sign_in("admin@example.com")).status_code == 200
    assert owed(await session_answer(browser)) == (False, False, True, "enrol")
    assert reason(await browser.get("/api/admin/whoami")) == "two_factor_required"
    assert reason(await browser.get("/api/auth/sessions")) == "two_factor_required"
    # The first factor is not a step-up for an admin: a password re-auth is refused by the gate.
    assert reason(await browser.post("/api/auth/reauth", {"password": "x" * 12})) == (
        "two_factor_required"
    )
    await enrol_through_the_api(browser)
    assert owed(await session_answer(browser)) == (True, True, True, None)
    assert (await browser.get("/api/admin/whoami")).status_code == 200
    assert notifier.enrolled == [user_id]


async def test_an_address_in_admin_emails_must_enrol_even_before_the_grant(
    app_database_url: str, clean_auth: None
) -> None:
    async with running_env(app_database_url, admin_emails=ADMIN_EMAIL) as env:
        user_id = await env.user(ADMIN_EMAIL)  # still role user: no sign-in granted it yet
        issued = await env.login(user_id)
        answer = await env.client.get("/api/auth/session", headers=session_cookie(issued.token))
        assert owed(answer.json()) == (False, False, True, "enrol")
        assert refused(
            await env.client.get("/api/auth/sessions", headers=session_cookie(issued.token))
        )
        # The purge and the session list treat it as owing its second factor too.
        await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")
        assert (await purge.purge_expired(env.factory, env.settings))["sessions"] == 1


async def test_an_admin_whose_totp_is_removed_goes_back_to_enrolment(
    env: Env, notifier: FakeNotifier
) -> None:
    user_id = await user_with_password(env, "admin@example.com", role="admin")
    browser = Browser(env)
    assert (await browser.sign_in("admin@example.com")).status_code == 200
    await pass_second_factor(env, browser, user_id)  # its code counts as the step-up
    assert (await browser.request("DELETE", "/api/auth/mfa/totp")).status_code == 204
    assert owed(await session_answer(browser)) == (False, False, True, "enrol")
    assert reason(await browser.get("/api/admin/whoami")) == "two_factor_required"
    assert notifier.removed == [user_id]


# --- a session that owes its second factor is short-lived --------------------------------------


async def test_a_session_owing_its_second_factor_ends_after_ten_minutes(env: Env) -> None:
    user_id = await env.user()
    await env.enrol(user_id)
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


async def test_the_purge_removes_stale_sessions_that_owe_their_second_factor(env: Env) -> None:
    assert purge.PENDING_WINDOW == sessions.PENDING_WINDOW
    user_id = await env.user()
    await env.enrol(user_id)
    stale = await env.login(user_id)
    fresh = await env.login(user_id)
    full = await env.full_login(user_id)
    no_totp = await env.login(await env.user("plain@example.com"))
    admin = await env.login(await env.user("admin@example.com", role="admin"))
    await env.run(
        "UPDATE sessions SET reauth_at = now() - interval '11 minutes'"
        " WHERE id IN (:a, :b, :c, :d)",
        a=stale.id,
        b=full.id,
        c=no_totp.id,
        d=admin.id,
    )
    removed = await purge.purge_expired(env.factory, env.settings)
    assert removed["sessions"] == 2  # the TOTP account's stale one and the admin's
    left = await env.scalar("SELECT string_agg(id::text, ',' ORDER BY id) FROM sessions")
    assert set(str(left).split(",")) == {str(fresh.id), str(full.id), str(no_totp.id)}


# --- turning the TOTP off ----------------------------------------------------------------------


async def test_turning_totp_off_keeps_the_user_signed_in_and_signs_out_the_rest(
    env: Env, notifier: FakeNotifier
) -> None:
    browser = await signed_in(env)  # its code counts as the step-up
    user_id = UUID(str(await env.scalar("SELECT id FROM users")))
    other = await env.full_login(user_id)
    assert (await browser.post("/api/auth/mfa/recovery-codes")).status_code == 200
    assert await env.scalar("SELECT count(*) FROM mfa_recovery_codes") == 10
    before = browser.cookies[SESSION_COOKIE]
    response = await browser.request("DELETE", "/api/auth/mfa/totp")
    assert response.status_code == 204
    assert browser.cookies[SESSION_COOKIE] != before  # rotated
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0
    assert await env.scalar("SELECT count(*) FROM mfa_recovery_codes") == 0
    assert await env.scalar("SELECT count(*) FROM sessions WHERE id = :i", i=other.id) == 0
    assert owed(await session_answer(browser)) == (True, False, False, None)
    assert (await browser.get("/api/auth/sessions")).status_code == 200
    assert notifier.removed == [user_id]
    kinds = await env.scalar("SELECT string_agg(kind, ',' ORDER BY at) FROM auth_events")
    assert str(kinds).endswith("totp_removed")


async def test_turning_totp_off_needs_a_step_up_with_the_totp(
    env: Env, notifier: FakeNotifier
) -> None:
    browser = await signed_in(env)
    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    # The password is no step-up for an account that has a TOTP.
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    assert reason(await browser.request("DELETE", "/api/auth/mfa/totp")) == "step_up_required"
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 1
    assert notifier.removed == []


async def test_the_session_answer_says_what_is_missing(env: Env) -> None:
    """The contract the SPA reads (frontend ``api/auth.ts``): ``totp_enabled``,
    ``totp_required`` and ``second_factor`` sit in the session answer, so no other call is
    needed to route."""
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
        "totp_required",
        "second_factor",
        "admin_pending_mfa",
    }
    assert owed(body) == (True, False, False, None)
    assert await env.enrol(user_id) == TEST_TOTP_SECRET
    body = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()
    assert owed(body) == (False, True, False, "verify")
    # A second factor passed for an authenticator that is gone: a user is full without it ...
    await env.run("UPDATE sessions SET mfa_at = now()")
    await env.run("DELETE FROM mfa_totp")
    body = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()
    assert owed(body) == (True, False, False, None)
    # ... an admin is not: it goes back to enrolment.
    await env.run("UPDATE users SET role = 'admin'")
    body = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()
    assert owed(body) == (False, False, True, "enrol")
