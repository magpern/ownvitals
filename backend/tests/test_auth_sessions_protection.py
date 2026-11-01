"""Session protection (#11, ownvitals ADR 0003 section 3): idle and absolute limits, rotation,
and log out everywhere. Every account is synthetic."""

from uuid import UUID

import httpx

from authtools import (  # noqa: F401 - fixtures
    ADMIN_EMAIL,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    running_env,
    session_cookie,
)
from flows import PASSWORD, Browser, pass_second_factor, reason, signed_in
from ownvitals.auth.cookies import SESSION_COOKIE
from ownvitals.config import Settings


def cleared(response: httpx.Response) -> bool:
    return any(
        cookie.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" in cookie
        for cookie in response.headers.get_list("set-cookie")
    )


async def sessions_of(env: Env) -> int:
    return int(str(await env.scalar("SELECT count(*) FROM sessions")))


def test_the_limits_are_72_hours_idle_and_30_days_absolute() -> None:
    settings = Settings()
    assert (settings.session_idle_hours, settings.session_absolute_days) == (72, 30)


# --- idle and absolute expiry ----------------------------------------------------------------


async def test_a_session_idle_for_the_limit_is_refused_and_deleted(env: Env) -> None:
    issued = await env.full_login(await env.user())
    await env.run("UPDATE sessions SET last_seen_at = now() - interval '71 hours'")
    ok = await env.client.get("/api/auth/sessions", headers=session_cookie(issued.token))
    assert ok.status_code == 200
    await env.run("UPDATE sessions SET last_seen_at = now() - interval '72 hours'")
    gone = await env.client.get("/api/auth/sessions", headers=session_cookie(issued.token))
    assert (gone.status_code, reason(gone)) == (401, "not_authenticated")
    assert cleared(gone)
    assert await sessions_of(env) == 0


async def test_the_idle_limit_is_a_setting(app_database_url: str, clean_auth: None) -> None:
    async with running_env(app_database_url, session_idle_hours=2) as env:
        issued = await env.full_login(await env.user())
        await env.run("UPDATE sessions SET last_seen_at = now() - interval '3 hours'")
        gone = await env.client.get("/api/auth/sessions", headers=session_cookie(issued.token))
        assert gone.status_code == 401


async def test_a_session_past_its_absolute_limit_is_refused_however_active(env: Env) -> None:
    issued = await env.full_login(await env.user())
    await env.run("UPDATE sessions SET expires_at = now() - interval '1 second'")
    gone = await env.client.get("/api/auth/sessions", headers=session_cookie(issued.token))
    assert gone.status_code == 401
    assert cleared(gone)
    assert await sessions_of(env) == 0


async def test_rotation_never_extends_the_absolute_limit(env: Env) -> None:
    browser = await signed_in(env)
    await env.run("UPDATE sessions SET expires_at = now() + interval '1 day'")
    before = await env.scalar("SELECT expires_at FROM sessions")
    reauth = await browser.post("/api/auth/reauth", {"password": PASSWORD})
    assert reauth.status_code == 200
    assert await env.scalar("SELECT expires_at FROM sessions") == before


# --- rotation -----------------------------------------------------------------------------------


async def rotates(env: Env, browser: Browser, method: str, path: str, body: object = None) -> None:
    old = browser.cookies[SESSION_COOKIE]
    response = await browser.request(method, path, body)
    assert response.status_code < 300, (path, response.text)
    new = browser.cookies[SESSION_COOKIE]
    assert new != old, path
    stale = await env.client.get("/api/auth/session", headers=session_cookie(old))
    assert stale.json()["authenticated"] is False, path  # the old token is dead at once


async def test_sign_in_rotates_the_session(env: Env) -> None:
    browser = await signed_in(env)
    old = browser.cookies[SESSION_COOKIE]
    assert (await browser.sign_in("user@example.com")).status_code == 200
    assert browser.cookies[SESSION_COOKIE] != old
    stale = await env.client.get("/api/auth/session", headers=session_cookie(old))
    assert stale.json()["authenticated"] is False
    assert await sessions_of(env) == 1


async def test_the_second_factor_rotates_the_session(env: Env) -> None:
    browser = await signed_in(env, second_factor=False)
    old = browser.cookies[SESSION_COOKIE]
    user_id = UUID(str(await env.scalar("SELECT id FROM users")))
    await pass_second_factor(env, browser, user_id)
    assert browser.cookies[SESSION_COOKIE] != old
    stale = await env.client.get("/api/auth/session", headers=session_cookie(old))
    assert stale.json()["authenticated"] is False


async def test_reauth_and_step_up_rotate_the_session(env: Env) -> None:
    from authtools import TEST_TOTP_SECRET
    from ownvitals.auth import totp

    browser = await signed_in(env)
    await rotates(env, browser, "POST", "/api/auth/reauth", {"password": PASSWORD})
    await env.run("UPDATE mfa_totp SET last_step = 0")
    code = totp.code_for_step(TEST_TOTP_SECRET, int(totp.clock() // totp.PERIOD))
    await rotates(env, browser, "POST", "/api/auth/mfa/stepup", {"code": code})


async def test_a_privilege_change_revokes_every_session(env: Env) -> None:
    """A role change (admin grant or revoke) signs the user out everywhere; the next sign-in is
    a new session that passes TOTP again."""
    from ownvitals.auth.accounts import grant_admin

    browser = await signed_in(env, ADMIN_EMAIL)
    async with env.db() as db:
        assert await grant_admin(db, ADMIN_EMAIL) is True
    assert await sessions_of(env) == 0
    gone = await browser.get("/api/auth/session")
    assert gone.json()["authenticated"] is False


async def test_sign_in_with_a_listed_admin_email_starts_without_the_second_factor(
    app_database_url: str, clean_auth: None
) -> None:
    async with running_env(app_database_url, admin_emails=ADMIN_EMAIL) as env:
        browser = await signed_in(env, ADMIN_EMAIL, second_factor=False)
        body = (await browser.get("/api/auth/session")).json()
        assert body["session"]["second_factor"] == "enrol"
        assert reason(await browser.get("/api/admin/whoami")) == "two_factor_required"


# --- sign-out and log out everywhere -----------------------------------------------------------


async def test_sign_out_deletes_only_this_session(env: Env) -> None:
    browser = await signed_in(env)
    user_id = UUID(str(await env.scalar("SELECT id FROM users")))
    await env.full_login(user_id)
    response = await browser.post("/api/auth/logout")
    assert response.status_code == 204
    assert await sessions_of(env) == 1


async def test_sign_out_works_without_the_second_factor(env: Env) -> None:
    browser = await signed_in(env, second_factor=False)
    assert (await browser.post("/api/auth/logout")).status_code == 204
    assert await sessions_of(env) == 0


async def test_log_out_everywhere_revokes_every_session_of_the_user_only(env: Env) -> None:
    browser = await signed_in(env)  # the code of this sign-in counts as the step-up
    user_id = UUID(str(await env.scalar("SELECT id FROM users")))
    mine = [await env.full_login(user_id) for _ in range(2)]
    stranger = await env.full_login(await env.user("other@example.com"))
    response = await browser.post("/api/auth/logout-all")
    assert response.status_code == 204
    assert cleared(response)
    assert await env.scalar("SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 0
    for issued in mine:
        gone = await env.client.get("/api/auth/sessions", headers=session_cookie(issued.token))
        assert gone.status_code == 401
    kept = await env.client.get("/api/auth/sessions", headers=session_cookie(stranger.token))
    assert kept.status_code == 200
