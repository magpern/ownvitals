"""The shorter session after a cross-device passkey sign-in (ADR 0011 section 3, amendment of
2026-11-08; #252, porting classbeat #776).

A sign-in whose authenticator is not this device's own (``cross-platform``: a phone over the
hybrid transport or a security key; no attachment; or a ``platform`` claim that the registered
transports contradict) gets a session of at most 24 hours absolute and 1 hour idle, carried over
every rotation, and no known-device cookie. Mandatory TOTP is unchanged. Synthetic keys and
accounts only.
"""

from datetime import timedelta

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from flows import Browser, only_user_id, pass_second_factor, signed_in
from ownvitals.auth.cookies import DEVICE_COOKIE, SESSION_COOKIE
from ownvitals.auth.purge import purge_expired
from ownvitals.auth.session_manage import list_sessions
from passkeytools import SoftAuthenticator, register, sign_in

EMAIL = "user@example.com"
DAY = timedelta(hours=24).total_seconds()


async def with_passkey(env: Env, transports: list[str] | None = None) -> SoftAuthenticator:
    """A user (password, TOTP) with one passkey; every session and known device is removed."""
    browser = await signed_in(env, EMAIL)
    auth = SoftAuthenticator()
    assert (await register(browser, auth, transports=transports)).status_code == 201
    await env.run("DELETE FROM sessions")
    await env.run("DELETE FROM known_devices")
    return auth


async def full(env: Env, browser: Browser) -> None:
    await env.run("UPDATE mfa_totp SET last_step = 0")
    await pass_second_factor(env, browser, await only_user_id(env))


async def life(env: Env) -> float:
    return float(await env.scalar("SELECT extract(epoch FROM expires_at - now()) FROM sessions"))  # pyright: ignore[reportArgumentType]


async def event_detail(env: Env) -> dict[str, object]:
    return await env.scalar("SELECT detail FROM auth_events WHERE kind = 'passkey_signin'")  # pyright: ignore[reportReturnType]


async def test_a_same_device_sign_in_keeps_the_normal_session_and_device(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    assert (await sign_in(browser, auth)).status_code == 200
    assert await env.scalar("SELECT origin FROM sessions") == "signin"
    assert await life(env) > timedelta(days=29).total_seconds()
    assert DEVICE_COOKIE in browser.cookies
    assert await env.scalar("SELECT count(*) FROM known_devices") == 1
    assert "cross_device" not in await event_detail(env)


async def test_a_hybrid_sign_in_gets_the_short_session_and_no_device(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    answer = await sign_in(browser, auth, attachment="cross-platform")
    assert answer.status_code == 200, answer.text
    assert await env.scalar("SELECT origin FROM sessions") == "passkey_cross_device"
    assert DAY - 60 < await life(env) <= DAY
    cookie = next(h for h in answer.headers.get_list("set-cookie") if SESSION_COOKIE in h)
    max_age = int(cookie.split("Max-Age=")[1].split(";")[0])
    assert DAY - 60 < max_age <= DAY
    assert DEVICE_COOKIE not in browser.cookies
    assert await env.scalar("SELECT count(*) FROM known_devices") == 0
    assert (await event_detail(env))["cross_device"] is True
    # Mandatory TOTP is unchanged: the session still owes its second factor.
    state = (await browser.get("/api/auth/session")).json()["session"]
    assert (state["mfa"], state["second_factor"]) == (False, "verify")


async def test_a_platform_claim_the_transports_contradict_fails_closed(env: Env) -> None:
    """Registered as a roaming authenticator (no ``internal``), so ``platform`` is not believed."""
    auth = await with_passkey(env, transports=["hybrid", "usb"])
    assert (await sign_in(Browser(env), auth, attachment="platform")).status_code == 200
    assert await env.scalar("SELECT origin FROM sessions") == "passkey_cross_device"
    assert await life(env) <= DAY


async def test_no_registered_transports_fails_closed(env: Env) -> None:
    auth = await with_passkey(env, transports=[])
    assert (await sign_in(Browser(env), auth, attachment="platform")).status_code == 200
    assert await env.scalar("SELECT origin FROM sessions") == "passkey_cross_device"


async def test_a_missing_attachment_fails_closed(env: Env) -> None:
    auth = await with_passkey(env)
    assert (await sign_in(Browser(env), auth, attachment=None)).status_code == 200
    assert await env.scalar("SELECT origin FROM sessions") == "passkey_cross_device"


async def test_a_rotation_keeps_the_shorter_limits(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    assert (await sign_in(browser, auth, attachment="cross-platform")).status_code == 200
    before = await env.scalar("SELECT expires_at FROM sessions")
    await full(env, browser)  # the TOTP verification rotates the session
    row = await env.row("SELECT origin, expires_at, mfa_at FROM sessions")
    assert (row.origin, row.expires_at) == ("passkey_cross_device", before)
    assert row.mfa_at is not None


async def test_a_cross_device_session_idles_out_after_an_hour(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    assert (await sign_in(browser, auth, attachment="cross-platform")).status_code == 200
    await full(env, browser)
    await env.run("UPDATE sessions SET last_seen_at = now() - interval '59 minutes'")
    assert (await browser.get("/api/auth/sessions")).status_code == 200
    await env.run("UPDATE sessions SET last_seen_at = now() - interval '61 minutes'")
    async with env.db() as db:
        assert await list_sessions(db, env.settings, await only_user_id(env)) == []
    assert (await browser.get("/api/auth/sessions")).status_code == 401
    assert await env.scalar("SELECT count(*) FROM sessions") == 0  # resolve deleted it


async def test_the_purge_uses_the_shorter_idle_limit(env: Env) -> None:
    auth = await with_passkey(env)
    assert (await sign_in(Browser(env), auth, attachment="cross-platform")).status_code == 200
    assert (await sign_in(Browser(env), auth)).status_code == 200
    await env.run("UPDATE sessions SET last_seen_at = now() - interval '61 minutes'")
    await purge_expired(env.factory, env.settings)
    assert await env.scalar("SELECT array_agg(origin) FROM sessions") == ["signin"]


async def test_a_same_device_session_does_not_idle_out_after_an_hour(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    assert (await sign_in(browser, auth)).status_code == 200
    await full(env, browser)
    await env.run("UPDATE sessions SET last_seen_at = now() - interval '61 minutes'")
    assert (await browser.get("/api/auth/sessions")).status_code == 200


async def test_an_inherited_shorter_expiry_is_kept(env: Env) -> None:
    """The browser's own valid session passes its expiry on; the cap never lengthens it."""
    auth = await with_passkey(env)
    browser = Browser(env)
    assert (await sign_in(browser, auth)).status_code == 200
    await full(env, browser)
    await env.run("UPDATE sessions SET expires_at = now() + interval '2 hours'")
    assert (await sign_in(browser, auth, attachment="cross-platform")).status_code == 200
    assert await life(env) <= timedelta(hours=2).total_seconds()
    assert await env.scalar("SELECT origin FROM sessions") == "passkey_cross_device"


async def test_a_long_session_of_the_browser_is_capped(env: Env) -> None:
    auth = await with_passkey(env)
    browser = Browser(env)
    assert (await sign_in(browser, auth)).status_code == 200
    await full(env, browser)
    assert await life(env) > timedelta(days=29).total_seconds()  # inherited by the next one
    assert (await sign_in(browser, auth, attachment="cross-platform")).status_code == 200
    assert await life(env) <= DAY
