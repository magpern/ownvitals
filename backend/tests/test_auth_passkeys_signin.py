"""Passkey sign-in (ADR 0011 sections 1, 3 and 4), with a software authenticator.

A passkey replaces the password, never the TOTP: the session it gives still owes its second
factor. Every refusal is the same 401 ``passkey_failed``, and each kind is audited with its
reason only. Every key and account is synthetic.
"""

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from flows import Browser, only_user_id, pass_second_factor, signed_in
from ownvitals.auth import ratelimit
from ownvitals.auth.cookies import PRE_COOKIE
from passkeytools import (
    BASE,
    UP,
    UV,
    SoftAuthenticator,
    failed,
    register,
    sign_in,
    signin_options,
)

EMAIL = "user@example.com"


async def with_passkey(env: Env, email: str = EMAIL) -> SoftAuthenticator:
    """A user (password, TOTP) with one registered passkey; the browser is thrown away."""
    browser = await signed_in(env, email)
    auth = SoftAuthenticator()
    assert (await register(browser, auth)).status_code == 201
    await env.run("DELETE FROM sessions")
    return auth


async def reasons(env: Env) -> list[str]:
    rows = await env.scalar(
        "SELECT array_agg(detail->>'reason' ORDER BY at) FROM auth_events "
        "WHERE kind = 'passkey_signin_failed'"
    )
    return list(rows or [])  # pyright: ignore[reportArgumentType]


async def test_a_passkey_sign_in_gives_a_session_that_still_owes_its_totp(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    answer = await sign_in(browser, auth)
    assert answer.status_code == 200, answer.text
    assert answer.json()["email"] == EMAIL
    assert browser.signed_in
    assert PRE_COOKIE not in browser.cookies  # deleted on sign-in
    assert await env.scalar("SELECT count(*) FROM pre_sessions") == 0
    state = (await browser.get("/api/auth/session")).json()["session"]
    assert (state["mfa"], state["second_factor"], state["stepup"]) == (False, "verify", False)
    gated = await browser.get("/api/auth/sessions")
    assert gated.json() == {"detail": {"reason": "two_factor_required"}}
    row = await env.row("SELECT mfa_at, stepup_at, reauth_at FROM sessions")
    assert row.mfa_at is None
    assert row.stepup_at is None
    assert row.reauth_at is not None
    await env.run("UPDATE mfa_totp SET last_step = 0")
    await pass_second_factor(env, browser, answer.json()["id"])
    assert (await browser.get("/api/auth/sessions")).status_code == 200
    assert await env.scalar("SELECT sign_count FROM webauthn_credentials") == 1
    assert await env.scalar("SELECT last_used_at IS NOT NULL FROM webauthn_credentials")
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'passkey_signin'") == 1


async def test_a_replayed_assertion_is_refused(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    options = await signin_options(browser)
    credential = auth.get(options)
    assert (await browser.post(f"{BASE}/signin", {"credential": credential})).status_code == 200
    again = Browser(env)
    again.cookies = {k: v for k, v in browser.cookies.items() if k != "__Host-ownvitals_session"}
    await signin_options(again)  # a live pre-session of its own
    assert failed(await again.post(f"{BASE}/signin", {"credential": credential}))
    assert "challenge" in await reasons(env)


BROKEN = {
    "foreign origin": ({"origin": "https://evil.test"}, "client_data"),
    "wrong rp id": ({"rp_id": "evil.test"}, "verification"),
    "wrong type": ({"kind": "webauthn.create"}, "client_data"),
    "unknown challenge": ({"challenge": "A" * 43}, "challenge"),
    "user verification missing": ({"flags": UP}, "verification"),
    "user presence missing": ({"flags": UV}, "verification"),
    "a bad signature": ({"bad_signature": True}, "verification"),
    "no user handle": ({"user_handle": None}, "user_handle"),
    "a cross-origin frame": ({"cross_origin": True}, "client_data"),
}


@pytest.mark.parametrize("broken", list(BROKEN))
async def test_each_broken_assertion_is_refused_with_the_generic_answer(
    env: Env, broken: str
) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    kwargs, why = BROKEN[broken]
    assert failed(await sign_in(browser, auth, **kwargs))
    assert not browser.signed_in
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert await reasons(env) == [why]
    assert await env.scalar("SELECT sign_count FROM webauthn_credentials") == 0


async def test_an_unknown_credential_is_refused_like_a_bad_signature(env: Env) -> None:
    await with_passkey(env)
    stranger = SoftAuthenticator()
    stranger.create({"user": {"id": "AAAA"}, "challenge": "AAAA"})  # never registered
    answer = await sign_in(Browser(env), stranger)
    assert failed(answer)
    assert await reasons(env) == ["unknown_credential"]


async def test_a_user_handle_of_another_account_is_refused(env: Env) -> None:
    auth = await with_passkey(env)
    other = await with_passkey(env, "other@example.com")
    handle = other.credentials[0].user_handle
    assert failed(await sign_in(Browser(env), auth, user_handle=handle))
    assert await reasons(env) == ["user_handle"]


async def test_a_challenge_of_another_pre_session_is_refused(env: Env) -> None:
    auth = await with_passkey(env)
    first, second = Browser(env), Browser(env)
    options = await signin_options(first)
    await signin_options(second)
    answer = await second.post(f"{BASE}/signin", {"credential": auth.get(options)})
    assert failed(answer)
    assert await reasons(env) == ["challenge"]


async def test_without_a_pre_session_cookie_the_assertion_is_refused(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    options = await signin_options(browser)
    browser.cookies.pop(PRE_COOKIE)
    assert failed(await browser.post(f"{BASE}/signin", {"credential": auth.get(options)}))


async def test_an_expired_challenge_is_refused(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    options = await signin_options(browser)
    await env.run("UPDATE webauthn_challenges SET expires_at = now() - interval '1 second'")
    assert failed(await browser.post(f"{BASE}/signin", {"credential": auth.get(options)}))


async def test_a_disabled_account_can_not_sign_in(env: Env) -> None:
    auth = await with_passkey(env)
    await env.run("UPDATE users SET disabled_at = now()")
    assert failed(await sign_in(Browser(env), auth))
    assert await reasons(env) == ["account"]


async def test_an_account_without_a_confirmed_totp_can_not_sign_in(env: Env) -> None:
    """ADR 0011 section 3: first enrolment is reachable through the mailed reset only."""
    auth = await with_passkey(env)
    await env.run("UPDATE mfa_totp SET confirmed_at = NULL")
    assert failed(await sign_in(Browser(env), auth))
    await env.run("DELETE FROM mfa_totp")
    assert failed(await sign_in(Browser(env), auth))
    assert await reasons(env) == ["no_totp", "no_totp"]
    assert await env.scalar("SELECT count(*) FROM sessions") == 0


async def test_a_suspended_credential_is_refused_before_its_signature(env: Env) -> None:
    auth = await with_passkey(env)
    await env.run("UPDATE webauthn_credentials SET suspended_at = now()")
    assert failed(await sign_in(Browser(env), auth))
    assert failed(await sign_in(Browser(env), auth, bad_signature=True))
    assert await reasons(env) == ["suspended", "suspended"]
    assert await env.scalar("SELECT sign_count FROM webauthn_credentials") == 0


async def test_a_malformed_credential_is_refused_and_counted(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    options = await signin_options(browser)
    credential = auth.get(options)
    credential["id"] = "AAAA"  # id and rawId disagree
    assert failed(await browser.post(f"{BASE}/signin", {"credential": credential}))
    assert await reasons(env) == ["client_data"]


async def test_failures_count_in_the_sign_in_buckets_and_a_success_releases_them(
    env: Env,
) -> None:
    auth = await with_passkey(env)

    async def total() -> int:
        return int(str(await env.scalar("SELECT coalesce(sum(count), 0) FROM rate_limits")))

    before = await total()
    stranger = SoftAuthenticator()
    stranger.create({"user": {"id": "AAAA"}, "challenge": "AAAA"})
    assert failed(await sign_in(Browser(env), stranger))  # unknown: the IP bucket only
    unknown = await total()
    assert unknown == before + 2  # + the options bucket
    assert failed(await sign_in(Browser(env), auth, bad_signature=True))
    known = await total()
    assert known == unknown + 4  # options, and pair, account and IP
    assert (await sign_in(Browser(env), auth)).status_code == 200
    # The success adds the options slot and releases its own three.
    assert await total() < known + 1


async def test_sign_in_options_are_limited_per_ip(env: Env) -> None:
    limit = ratelimit.PASSKEY_OPTIONS_IP
    assert (limit.action, limit.scope, limit.max_count, limit.window_seconds) == (
        "passkey-options",
        "ip",
        20,
        600,
    )
    browser = Browser(env)
    for _ in range(20):
        assert (await browser.post(f"{BASE}/signin/options")).status_code == 200
    refused = await browser.post(f"{BASE}/signin/options")
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0


async def test_a_full_session_may_sign_in_again_and_its_old_session_goes(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    assert (await browser.sign_in(EMAIL)).status_code == 200
    await env.run("UPDATE mfa_totp SET last_step = 0")
    await pass_second_factor(env, browser, await only_user_id(env))
    old = browser.cookies["__Host-ownvitals_session"]
    assert (await sign_in(browser, auth)).status_code == 200
    assert browser.cookies["__Host-ownvitals_session"] != old
    assert await env.scalar("SELECT count(*) FROM sessions WHERE mfa_at IS NOT NULL") == 0
