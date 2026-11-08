"""Passkey hardening ported from classbeat #795 (#252): a challenge of the other purpose is
refused both ways, and the disabled check inside the final, locked transaction holds. Synthetic
keys and accounts only."""

import asyncio

from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from flows import Browser, signed_in
from passkeytools import BASE, SoftAuthenticator, failed, register, signin_options

EMAIL = "user@example.com"


async def reasons(env: Env) -> list[str]:
    rows = await env.scalar(
        "SELECT array_agg(detail->>'reason' ORDER BY at) FROM auth_events "
        "WHERE kind = 'passkey_signin_failed'"
    )
    return list(rows or [])  # pyright: ignore[reportArgumentType]


async def test_a_reauth_challenge_is_refused_at_sign_in(env: Env) -> None:
    browser = await signed_in(env, EMAIL)
    auth = SoftAuthenticator()
    assert (await register(browser, auth)).status_code == 201
    reauth = (await browser.post(f"{BASE}/reauth/options")).json()
    anonymous = Browser(env)
    await signin_options(anonymous)  # a pre-session of its own
    answer = await anonymous.post(f"{BASE}/signin", {"credential": auth.get(reauth)})
    assert failed(answer)
    assert await reasons(env) == ["challenge"]


async def test_a_sign_in_challenge_is_refused_at_reauth(env: Env) -> None:
    browser = await signed_in(env, EMAIL)
    auth = SoftAuthenticator()
    assert (await register(browser, auth)).status_code == 201
    signin = await signin_options(browser)
    answer = await browser.post(f"{BASE}/reauth", {"credential": auth.get(signin)})
    assert failed(answer)
    assert await reasons(env) == ["challenge"]


async def test_an_account_disabled_while_the_sign_in_waits_gets_no_session(env: Env) -> None:
    """The check before the signature passed; the account is disabled while the sign-in waits
    for the account row, and the final transaction's own check refuses it."""
    browser = await signed_in(env, EMAIL)
    auth = SoftAuthenticator()
    assert (await register(browser, auth)).status_code == 201
    await env.run("DELETE FROM sessions")
    anonymous = Browser(env)
    credential = auth.get(await signin_options(anonymous))
    async with env.db() as db:
        await db.execute(text("SELECT 1 FROM users FOR NO KEY UPDATE"))
        attempt = asyncio.create_task(anonymous.post(f"{BASE}/signin", {"credential": credential}))
        await asyncio.sleep(0.5)
        assert not attempt.done()  # waiting for the account row
        await db.execute(text("UPDATE users SET disabled_at = now()"))
    assert failed(await attempt)
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert await reasons(env) == ["account"]
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'passkey_signin'") == 0
