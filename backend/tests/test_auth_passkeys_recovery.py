"""Account recovery and passkeys (ADR 0011 section 3; the #165 hardening items for ADR 0011).

``admin mfa-reset`` removes every passkey and every linked Google identity with the TOTP, in one
transaction that locks the account row first, and its mail says so; afterwards the only way in
is the mailed reset link. A password reset keeps passkeys and its mail lists them. A passkey
sign-in or registration racing a reset never leaves a session or a passkey behind. Synthetic
accounts and keys only.
"""

import asyncio

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    smtp_fixture,
)
from flows import Browser, signed_in
from ownvitals.auth import accounts, operator
from passkeytools import BASE, SoftAuthenticator, failed, register, register_options, sign_in
from smtpfake import SmtpFake

EMAIL = "user@example.com"


async def with_passkey(env: Env) -> tuple[Browser, SoftAuthenticator]:
    browser = await signed_in(env, EMAIL)
    auth = SoftAuthenticator()
    assert (await register(browser, auth, name="Laptop")).status_code == 201
    return browser, auth


async def test_mfa_reset_removes_passkeys_and_google_and_says_so(menv: Env, smtp: SmtpFake) -> None:
    _, auth = await with_passkey(menv)
    await menv.run(
        "INSERT INTO identities (user_id, provider, subject) "
        "SELECT id, 'google', 'synthetic-sub' FROM users"
    )
    browser = Browser(menv)
    await browser.post(f"{BASE}/signin/options")  # an open sign-in challenge
    assert await operator.mfa_reset(menv.factory, menv.settings, EMAIL) is True
    for table in (
        "webauthn_credentials", "identities", "mfa_totp", "sessions", "known_devices",
    ):  # fmt: skip
        count = await menv.scalar(f"SELECT count(*) FROM {table}")  # noqa: S608
        assert count == 0, table
    detail = await menv.scalar("SELECT detail FROM auth_events WHERE kind = 'mfa_reset'")
    assert detail == {"source": "cli", "passkeys_removed": 1, "identities_removed": 1}
    [mail] = [m for m in smtp.to(EMAIL) if "Reset" in m.subject]
    assert "passkeys were removed" in mail.text
    assert "Google sign-in was unlinked" in mail.text
    assert "Laptop" not in mail.text  # nothing is kept, so nothing is listed
    assert failed(await sign_in(Browser(menv), auth))


async def test_a_password_reset_keeps_passkeys_and_its_mail_lists_them(
    menv: Env, smtp: SmtpFake
) -> None:
    await with_passkey(menv)
    answer = await Browser(menv).post("/api/auth/password/reset/request", {"email": EMAIL})
    assert answer.status_code == 202
    for _ in range(50):  # the mail goes out after the response
        if smtp.to(EMAIL):
            break
        await asyncio.sleep(0.02)
    [mail] = [m for m in smtp.to(EMAIL) if "Reset" in m.subject]
    assert "keeps these passkeys" in mail.text
    assert "- Laptop (added " in mail.text
    assert "/settings/security" in mail.text
    done = await Browser(menv).post(
        "/api/auth/password/reset/complete",
        {"token": mail.token(), "password": "another long synthetic password"},
    )
    assert done.status_code == 200, done.text
    assert await menv.scalar("SELECT count(*) FROM webauthn_credentials") == 1


async def test_a_sign_in_racing_a_reset_leaves_no_session(env: Env) -> None:
    """The reset holds the account row; the sign-in waits for it, then finds no credential."""
    _, auth = await with_passkey(env)
    await env.run("DELETE FROM sessions")
    browser = Browser(env)
    options = (await browser.post(f"{BASE}/signin/options")).json()
    credential = auth.get(options)
    async with env.db() as db:
        await accounts.reset_mfa(db, EMAIL)  # holds the lock until this block commits
        attempt = asyncio.create_task(browser.post(f"{BASE}/signin", {"credential": credential}))
        await asyncio.sleep(0.5)
        assert not attempt.done()  # waiting for the account row
    assert failed(await attempt)
    assert await env.scalar("SELECT count(*) FROM sessions") == 0


async def test_a_registration_racing_a_reset_leaves_no_passkey(env: Env) -> None:
    browser, _ = await with_passkey(env)
    await env.run("DELETE FROM webauthn_credentials")
    options = await register_options(browser)
    credential = SoftAuthenticator().create(options)
    csrf = await browser.csrf()
    async with env.db() as db:
        await accounts.reset_mfa(db, EMAIL)
        attempt = asyncio.create_task(
            browser.post(f"{BASE}/register", {"credential": credential}, csrf=csrf)
        )
        await asyncio.sleep(0.5)
    answer = await attempt
    assert answer.status_code in (400, 401, 403)
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 0


async def test_keys_wipe_removes_passkeys_too(env: Env) -> None:
    await with_passkey(env)
    user_id = await env.scalar("SELECT id FROM users")
    async with env.db() as db:
        result = await operator.wipe_user(db, user_id, ())  # pyright: ignore[reportArgumentType]
    assert result is not None
    assert await env.scalar("SELECT count(*) FROM webauthn_credentials") == 0
