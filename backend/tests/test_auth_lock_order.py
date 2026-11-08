"""The rest of the lock order of ``auth/userlock.py`` (#248): rotations and Google sign-in vs
reset and disable, TOTP removal vs ``admin mfa-reset``, and the facts the reset's keep list and
the re-checks rest on. Each test fails if the lock or re-check it names is removed. Synthetic
accounts only, on the real database."""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture, smtp_fixture  # noqa: F401 - fixtures
from flows import PASSWORD, Browser, reason, signed_in, user_with_password
from oauthtools import fake_fixture, oenv_fixture, sign_in_with  # noqa: F401 - fixtures
from oidcfake import FakeProviders
from ownvitals.auth import (
    accounts,
    admin_accounts,
    email_routes,
    mfa,
    oauth_outcomes,
    reset_routes,
    signin_routes,
)
from ownvitals.auth.events import record_event
from ownvitals.auth.oauth_store import add_identity
from ownvitals.auth.session_model import SessionGone
from ownvitals.auth.sessions import resolve_session, rotate_session
from ownvitals.auth.tokens import EMAIL_CHANGE_LIFETIME, issue_token
from racetools import EMAIL, NEW, WAIT, Held, after, complete_reset, reset_token, sessions
from smtpfake import SmtpFake

SUB = "google-subject"


async def google_user(env: Env) -> tuple[UUID, str]:
    """A user with a password and a linked Google identity; returns ``(user id, identity id)``."""
    user_id = await user_with_password(env)
    identity = await env.scalar(
        "INSERT INTO identities (user_id, provider, subject, created_at) "
        "VALUES (:u, 'google', :s, now() - interval '1 minute') RETURNING id",
        u=user_id,
        s=SUB,
    )
    return user_id, str(identity)


async def identities(env: Env) -> object:
    return await env.scalar("SELECT count(*) FROM identities")


# --- rotations (MFA verify, step-up, re-auth) vs reset ------------------------------------------


async def test_a_reset_waits_for_a_rotation_in_flight_and_revokes_the_new_session(
    env: Env,
) -> None:
    user_id = await user_with_password(env)
    issued = await env.login(user_id)
    info = (await resolve_session(env.factory, env.settings, issued.token)).info
    assert info is not None
    token = await reset_token(env, user_id)
    async with env.db() as db:
        await rotate_session(db, info, stepup=True)  # holds the user row until the commit
        resetting = asyncio.create_task(complete_reset(env, token))
        await asyncio.sleep(WAIT)
        assert not resetting.done()
    assert await resetting == 200
    assert await sessions(env) == 0  # the rotated row did not slip past the reset's DELETE


async def test_a_rotation_waiting_for_a_reset_finds_its_session_gone(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await user_with_password(env)
    issued = await env.login(user_id)
    info = (await resolve_session(env.factory, env.settings, issued.token)).info
    assert info is not None
    token = await reset_token(env, user_id)
    held = Held(monkeypatch, reset_routes, "revoke_user_sessions")
    resetting = asyncio.create_task(complete_reset(env, token))
    await held.inside()

    async def rotate() -> None:
        async with env.db() as db:
            await rotate_session(db, info, stepup=True)

    rotating = asyncio.create_task(rotate())
    await asyncio.sleep(WAIT)
    assert not rotating.done()
    held.release.set()
    assert await resetting == 200
    with pytest.raises(SessionGone):
        await rotating
    assert await sessions(env) == 0


# --- Google sign-in vs reset and disable --------------------------------------------------------


async def test_a_reset_waits_for_a_google_sign_in_in_flight(
    oenv: Env, fake: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, identity = await google_user(oenv)
    token = await reset_token(oenv, user_id)
    held = Held(monkeypatch, oauth_outcomes, "remember_device")  # after start_session, same tx
    signing_in = asyncio.create_task(sign_in_with(Browser(oenv), fake, "google", sub=SUB))
    await held.inside()
    resetting = asyncio.create_task(complete_reset(oenv, token, [identity]))
    await asyncio.sleep(WAIT)
    assert not resetting.done()  # waiting for the user row the sign-in holds
    held.release.set()
    await signing_in
    assert await resetting == 200
    assert await identities(oenv) == 0
    assert await sessions(oenv) == 0  # no session from a method the reset removed


async def test_a_google_sign_in_after_the_reset_removed_its_identity_is_refused(
    oenv: Env, fake: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, identity = await google_user(oenv)
    token = await reset_token(oenv, user_id)

    async def reset() -> None:
        assert await complete_reset(oenv, token, [identity]) == 200

    after(monkeypatch, oauth_outcomes, "identity_owner", reset)  # the read before the lock
    browser = Browser(oenv)
    await sign_in_with(browser, fake, "google", sub=SUB)
    assert not browser.signed_in
    assert await sessions(oenv) == 0


async def test_a_disable_waits_for_a_google_sign_in_in_flight(
    oenv: Env, fake: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, _ = await google_user(oenv)
    held = Held(monkeypatch, oauth_outcomes, "remember_device")
    signing_in = asyncio.create_task(sign_in_with(Browser(oenv), fake, "google", sub=SUB))
    await held.inside()
    disabling = asyncio.create_task(admin_accounts.disable_account(oenv.factory, user_id))
    await asyncio.sleep(WAIT)
    assert not disabling.done()
    held.release.set()
    await signing_in
    assert (await disabling)[1] == 1
    assert await sessions(oenv) == 0  # enable_account can bring back nothing


async def test_a_google_sign_in_after_a_disable_is_refused(
    oenv: Env, fake: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, _ = await google_user(oenv)

    async def disable() -> None:
        await admin_accounts.disable_account(oenv.factory, user_id)

    after(monkeypatch, oauth_outcomes, "identity_owner", disable)
    browser = Browser(oenv)
    await sign_in_with(browser, fake, "google", sub=SUB)
    assert not browser.signed_in
    assert await sessions(oenv) == 0


# --- TOTP removal vs admin mfa-reset (the user-side MFA reset) ----------------------------------


async def test_totp_removal_and_mfa_reset_serialise_without_a_deadlock(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``remove_totp`` locks the user row before the TOTP row, as ``reset_mfa`` does. Taking the
    TOTP row first would deadlock with the reset once its rotation locks the user row."""
    browser = await signed_in(env, EMAIL)  # the TOTP verify counts as step-up
    held = Held(monkeypatch, mfa, "remove_totp")
    csrf = await browser.csrf()
    removing = asyncio.create_task(browser.request("DELETE", "/api/auth/mfa/totp", csrf=csrf))
    await held.inside()

    async def reset() -> None:
        async with env.db() as db:
            await accounts.reset_mfa(db, EMAIL)

    resetting = asyncio.create_task(reset())
    await asyncio.sleep(WAIT)
    assert not resetting.done()
    held.release.set()
    assert (await removing).status_code == 204
    await resetting  # no deadlock error on either side
    assert await sessions(env) == 0
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0


# --- the keep list: the reset request's lock and the link's timestamp ---------------------------


async def reset_mail_token(smtp: SmtpFake) -> str:
    for _ in range(100):
        for message in smtp.to(EMAIL):
            if "/reset#token=" in message.text:
                return message.token()
        await asyncio.sleep(0.05)
    raise AssertionError("no reset mail")


async def test_a_reset_request_waits_for_a_link_in_flight_so_the_page_lists_it(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reset_mail`` locks before it issues the token: a link in flight commits first, and the
    reset page lists it (so keeping it is the user's choice)."""
    browser = await signed_in(oenv, EMAIL)
    held = Held(monkeypatch, oauth_outcomes, "add_identity")
    linking = asyncio.create_task(sign_in_with(browser, fake, "google", purpose="link", sub=SUB))
    await held.inside()
    requesting = asyncio.create_task(
        Browser(oenv).post("/api/auth/password/reset/request", {"email": EMAIL})
    )
    await asyncio.sleep(WAIT)
    assert not requesting.done()  # waiting for the user row the link holds
    held.release.set()
    await linking
    assert (await requesting).status_code == 202
    token = await reset_mail_token(smtp)
    info = await Browser(oenv).post("/api/auth/password/reset/info", {"token": token})
    assert [m["provider"] for m in info.json()["methods"]] == ["google"]
    assert await complete_reset(oenv, token) == 200
    assert await identities(oenv) == 1  # listed and not chosen: kept


async def test_a_link_whose_transaction_began_before_the_reset_link_is_removed(
    env: Env,
) -> None:
    """``created_at`` is the time under the lock (``clock_timestamp()``), not the start of the
    link's transaction: a link that began before the reset link was issued but inserted after it
    was never listed, and goes."""
    user_id = await user_with_password(env)
    issued = await env.login(user_id)
    async with env.db() as db:
        await db.execute(text("SELECT 1"))  # the transaction, and its now(), start here
        await asyncio.sleep(0.05)
        token = await reset_token(env, user_id)  # committed: issued after that now()
        await asyncio.sleep(0.05)
        assert await add_identity(db, user_id, "google", SUB, None, session_id=issued.id)
    assert await complete_reset(env, token) == 200
    assert await identities(env) == 0


# --- the re-checks under the lock ---------------------------------------------------------------


async def test_a_password_change_between_the_reauth_check_and_the_rotation_refuses_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change keeps this very session, so only the hash re-check under the lock stops the
    re-auth from proving a password that no longer holds."""
    browser = await signed_in(env, EMAIL)
    before = await env.scalar("SELECT reauth_at FROM sessions")

    async def change() -> None:
        changed = await browser.post("/api/auth/password/change", {"new_password": NEW})
        assert changed.status_code == 200

    after(monkeypatch, signin_routes, "_check_password", change)

    response = await browser.post("/api/auth/reauth", {"password": PASSWORD})
    assert (response.status_code, reason(response)) == (401, "invalid_credentials")
    assert await env.scalar("SELECT reauth_at FROM sessions") == before  # not rotated


async def test_a_password_change_waits_for_a_sign_in_in_flight_and_revokes_it(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await signed_in(env, EMAIL)
    held = Held(monkeypatch, signin_routes, "remember_device")
    signing_in = asyncio.create_task(Browser(env).sign_in(EMAIL))
    await held.inside()
    csrf = await owner.csrf()
    changing = asyncio.create_task(
        owner.post("/api/auth/password/change", {"new_password": NEW}, csrf=csrf)
    )
    await asyncio.sleep(WAIT)
    assert not changing.done()  # the user row first
    held.release.set()
    assert (await signing_in).status_code == 200
    assert (await changing).status_code == 200
    assert await sessions(env) == 1  # only the changer's own session


async def test_the_admin_disable_lock_lets_a_foreign_key_check_through(env: Env) -> None:
    """``_locked`` takes ``FOR NO KEY UPDATE``: a transaction that holds a session row and then
    inserts an ``auth_events`` row for the user (a ``FOR KEY SHARE`` check) is not blocked by the
    disable that waits for its session row. ``FOR UPDATE`` deadlocks here."""
    user_id = await user_with_password(env)
    issued = await env.login(user_id)
    async with env.db() as db:
        await db.execute(
            text("UPDATE sessions SET last_seen_at = now() WHERE id = :i"), {"i": issued.id}
        )
        disabling = asyncio.create_task(admin_accounts.disable_account(env.factory, user_id))
        await asyncio.sleep(WAIT)
        assert not disabling.done()  # it holds the user row and waits for the session row
        await asyncio.wait_for(record_event(db, "sign_in", user_id=user_id), 5)
    assert (await disabling)[1] == 1
    assert await sessions(env) == 0


async def test_an_email_change_and_a_reset_serialise_without_a_deadlock(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The email confirm locks the user row before it consumes its token. Token first, it would
    wait for the reset's user row while the reset waits to delete that token row."""
    browser = await signed_in(env, EMAIL)
    user_id = await env.scalar("SELECT id FROM users")
    async with env.db() as db:
        change = await issue_token(
            db,
            email_routes.PURPOSE,
            user_id,  # pyright: ignore[reportArgumentType]
            EMAIL_CHANGE_LIFETIME,
            new_email="new@example.com",
            email_at_issue=EMAIL,
        )
    reset = await reset_token(env, user_id)  # pyright: ignore[reportArgumentType]
    held = Held(monkeypatch, email_routes, "consume_token")
    csrf = await browser.csrf()
    confirming = asyncio.create_task(
        browser.post("/api/auth/email/confirm", {"token": change}, csrf=csrf)
    )
    await held.inside()
    resetting = asyncio.create_task(complete_reset(env, reset))
    await asyncio.sleep(WAIT)
    assert not resetting.done()
    held.release.set()
    assert (await confirming).status_code == 200
    # No deadlock error on either side: the confirm committed first and, as every email change
    # does, invalidated the user's other tokens, so the reset link is gone (400, not a 500).
    assert await resetting == 400
    assert await sessions(env) == 1  # the confirming session stays, as after any email change
