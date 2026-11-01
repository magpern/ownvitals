"""Password sign-in and re-authentication (ADR 0003 sections 3, 5, 6, 9)."""

from authtools import (  # noqa: F401 - fixtures
    ADMIN_EMAIL,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    running_env,
)
from flows import PASSWORD, Browser, age_reauth, reason, signed_in, user_with_password
from ownvitals.auth.cookies import DEVICE_COOKIE, SESSION_COOKIE

EMAIL = "user@example.com"


async def test_sign_in_sets_the_session_and_device_cookies(env: Env) -> None:
    user_id = await user_with_password(env)
    browser = Browser(env)
    response = await browser.sign_in(EMAIL)
    assert response.status_code == 200
    assert response.json()["email"] == EMAIL
    assert browser.signed_in
    assert DEVICE_COOKIE in browser.cookies
    set_cookie = " ".join(response.headers.get_list("set-cookie"))
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    session = (await browser.get("/api/auth/session")).json()
    assert session["authenticated"]
    assert session["session"]["recent_auth"]
    assert await env.scalar("SELECT count(*) FROM known_devices WHERE user_id = :u", u=user_id) == 1
    assert await env.scalar("SELECT kind FROM auth_events") == "sign_in"


async def test_every_failure_gets_the_same_answer(env: Env) -> None:
    await user_with_password(env, "has@example.com")
    await env.user("nopw@example.com")  # no password (provider-only)
    disabled = await user_with_password(env, "off@example.com")
    await env.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=disabled)
    cases = [
        ("has@example.com", "wrong password 12345"),
        ("unknown@example.com", PASSWORD),
        ("nopw@example.com", PASSWORD),
        ("off@example.com", PASSWORD),  # right password, disabled
        ("has@example.com", "short"),
    ]
    seen = set()
    for email, password in cases:
        browser = Browser(env)
        response = await browser.sign_in(email, password)
        assert response.status_code == 401
        assert reason(response) == "invalid_credentials"
        assert not browser.signed_in
        seen.add(response.text)
    assert len(seen) == 1
    assert await env.scalar("SELECT count(*) FROM sessions") == 0  # no session for a disabled user


async def test_an_unknown_email_still_costs_one_verification(env: Env) -> None:
    calls: list[str] = []
    hasher = env.app.state.password_hasher
    original = hasher.verify_dummy

    async def spy(password: str) -> None:
        calls.append(password)
        await original(password)

    hasher.verify_dummy = spy
    await env.user("nopw@example.com")
    await Browser(env).sign_in("unknown@example.com", PASSWORD)
    assert len(calls) == 1


async def test_failures_count_and_the_limit_answers_429_before_checking(env: Env) -> None:
    await user_with_password(env)
    browser = Browser(env)
    statuses = [
        (await browser.sign_in(EMAIL, "wrong password 12345")).status_code for _ in range(10)
    ]
    assert statuses == [401] * 10
    blocked = await browser.sign_in(EMAIL, PASSWORD)  # even the right password
    assert blocked.status_code == 429
    assert "retry-after" in blocked.headers
    assert not browser.signed_in


async def test_a_success_releases_what_it_reserved(env: Env) -> None:
    await user_with_password(env)
    browser = Browser(env)
    for _ in range(9):
        await browser.sign_in(EMAIL, "wrong password 12345")
    assert (await browser.sign_in(EMAIL)).status_code == 200
    for _ in range(9):  # the pair bucket was deleted by the success
        assert (await browser.sign_in(EMAIL, "wrong password 12345")).status_code == 401


async def test_sign_in_replaces_the_browsers_old_session(env: Env) -> None:
    browser = await signed_in(env)
    old = browser.cookies[SESSION_COOKIE]
    await browser.sign_in(EMAIL)
    assert browser.cookies[SESSION_COOKIE] != old
    assert await env.scalar("SELECT count(*) FROM sessions") == 1


async def test_a_listed_admin_gets_the_role_at_sign_in(
    app_database_url: str, clean_auth: None
) -> None:
    async with running_env(app_database_url, admin_emails=ADMIN_EMAIL) as env:
        await user_with_password(env, ADMIN_EMAIL)
        browser = Browser(env)
        assert (await browser.sign_in(ADMIN_EMAIL)).json()["role"] == "admin"
        assert await env.scalar("SELECT count(*) FROM sessions") == 1
        # The role takes effect only after TOTP (section 2).
        assert (await browser.get("/api/auth/session")).json()["user"]["role"] == "user"


async def test_reauth_sets_reauth_at_and_rotates_the_session(env: Env) -> None:
    browser = await signed_in(env)
    await age_reauth(env)
    assert (await browser.get("/api/auth/session")).json()["session"]["recent_auth"] is False
    old = browser.cookies[SESSION_COOKIE]
    response = await browser.post("/api/auth/reauth", {"password": PASSWORD})
    assert response.status_code == 200
    assert browser.cookies[SESSION_COOKIE] != old
    assert (await browser.get("/api/auth/session")).json()["session"]["recent_auth"] is True
    stale = Browser(env)
    stale.cookies[SESSION_COOKIE] = old
    assert not (await stale.get("/api/auth/session")).json()["authenticated"]


async def test_reauth_with_a_wrong_password_fails_generically_and_counts(env: Env) -> None:
    browser = await signed_in(env)
    wrong = await browser.post("/api/auth/reauth", {"password": "wrong password 12345"})
    assert (wrong.status_code, reason(wrong)) == (401, "invalid_credentials")
    assert browser.signed_in
    for _ in range(9):
        await browser.post("/api/auth/reauth", {"password": "wrong password 12345"})
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 429


async def test_reauth_needs_a_session(env: Env) -> None:
    response = await Browser(env).post("/api/auth/reauth", {"password": PASSWORD})
    assert response.status_code == 401
