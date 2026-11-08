"""Reset, sign-in, link, sign-out-everywhere and disable races (#248, ``auth/userlock.py``).

Two kinds of window, on the real database:

- *between transactions*: the password check (or the session read) and the final write run in
  separate transactions, and the other side commits in between. A hook runs the other side at
  that point, and the finish must notice under the user-row lock;
- *in flight*: both final transactions overlap. One is held inside its transaction after it took
  the lock (``held``), and the other must wait for it (not ``done``) and then undo or refuse it.

Whatever the order, no session or identity survives, and no deadlock or serialization error
reaches the user. Synthetic accounts only.
"""

import asyncio
from typing import Any

import pytest

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture, smtp_fixture  # noqa: F401 - fixtures
from flows import Browser, signed_in, user_with_password
from oauthtools import fake_fixture, oenv_fixture, sign_in_with  # noqa: F401 - fixtures
from oidcfake import FakeProviders
from ownvitals.auth import admin_accounts, oauth_outcomes, reset_routes, signin_routes
from racetools import EMAIL, LINKED, WAIT, Held, after, complete_reset, reset_token, sessions

# --- password sign-in vs reset ------------------------------------------------------------------


async def test_a_reset_between_the_password_check_and_the_session_wins(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await user_with_password(env)
    token = await reset_token(env, user_id)

    async def reset() -> None:
        assert await complete_reset(env, token) == 200

    after(monkeypatch, signin_routes, "_check_password", reset)
    browser = Browser(env)
    response = await browser.sign_in(EMAIL)
    assert response.status_code == 401  # the old password no longer holds under the lock
    assert not browser.signed_in
    assert await sessions(env) == 0


async def test_a_reset_waits_for_a_sign_in_in_flight_and_revokes_its_session(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await user_with_password(env)
    token = await reset_token(env, user_id)
    held = Held(monkeypatch, signin_routes, "remember_device")  # after start_session, same tx
    browser = Browser(env)
    signing_in = asyncio.create_task(browser.sign_in(EMAIL))
    await held.inside()
    resetting = asyncio.create_task(complete_reset(env, token))
    await asyncio.sleep(WAIT)
    assert not resetting.done()  # waiting for the user row the sign-in holds
    held.release.set()
    assert (await signing_in).status_code == 200
    assert await resetting == 200
    assert await sessions(env) == 0
    assert (await browser.get("/api/auth/sessions")).status_code == 401


async def test_a_sign_in_waits_for_a_reset_in_flight_and_is_refused(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await user_with_password(env)
    token = await reset_token(env, user_id)
    held = Held(monkeypatch, reset_routes, "revoke_user_sessions")
    resetting = asyncio.create_task(complete_reset(env, token))
    await held.inside()
    signing_in = asyncio.create_task(Browser(env).sign_in(EMAIL))
    await asyncio.sleep(WAIT)
    assert not signing_in.done()  # its final transaction waits for the user row
    held.release.set()
    assert await resetting == 200
    assert (await signing_in).status_code == 401
    assert await sessions(env) == 0


# --- link vs reset ------------------------------------------------------------------------------


async def test_an_identity_linked_after_the_reset_page_was_shown_does_not_survive(
    oenv: Env, fake: FakeProviders
) -> None:
    attacker = await signed_in(oenv, EMAIL)
    user_id = await oenv.scalar("SELECT id FROM users")
    token = await reset_token(oenv, user_id)  # pyright: ignore[reportArgumentType]
    info = await Browser(oenv).post("/api/auth/password/reset/info", {"token": token})
    assert info.json()["methods"] == []
    response = await sign_in_with(attacker, fake, "google", purpose="link", sub="late-sub")
    assert response.headers["location"] == LINKED
    assert await complete_reset(oenv, token) == 200  # removes nothing the page listed
    assert await oenv.scalar("SELECT count(*) FROM identities") == 0
    assert await sessions(oenv) == 0


async def test_an_identity_linked_before_the_reset_link_is_kept_unless_chosen(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await signed_in(oenv, EMAIL)
    for sub in ("keep-sub", "drop-sub"):
        await sign_in_with(browser, fake, "google", purpose="link", sub=sub)
    user_id = await oenv.scalar("SELECT id FROM users")
    await oenv.run("UPDATE identities SET created_at = now() - interval '1 minute'")
    token = await reset_token(oenv, user_id)  # pyright: ignore[reportArgumentType]
    drop = await oenv.scalar("SELECT id FROM identities WHERE subject = 'drop-sub'")
    assert await complete_reset(oenv, token, [str(drop)]) == 200
    assert await oenv.scalar("SELECT subject FROM identities") == "keep-sub"


async def test_a_link_finishing_after_a_reset_committed_is_refused(
    oenv: Env, fake: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The callback read the session before the reset; the link's insert comes after it."""
    attacker = await signed_in(oenv, EMAIL)
    user_id = await oenv.scalar("SELECT id FROM users")
    token = await reset_token(oenv, user_id)  # pyright: ignore[reportArgumentType]
    real = oauth_outcomes.add_identity

    async def reset_first(*args: Any, **kwargs: Any) -> bool:
        assert await complete_reset(oenv, token) == 200
        return await real(*args, **kwargs)

    monkeypatch.setattr(oauth_outcomes, "add_identity", reset_first)
    response = await sign_in_with(attacker, fake, "google", purpose="link", sub="stale")
    assert response.headers["location"] != LINKED
    assert await oenv.scalar("SELECT count(*) FROM identities") == 0
    assert await sessions(oenv) == 0


async def test_a_reset_waits_for_a_link_in_flight_and_removes_the_identity(
    oenv: Env, fake: FakeProviders, monkeypatch: pytest.MonkeyPatch
) -> None:
    attacker = await signed_in(oenv, EMAIL)
    user_id = await oenv.scalar("SELECT id FROM users")
    token = await reset_token(oenv, user_id)  # pyright: ignore[reportArgumentType]
    held = Held(monkeypatch, oauth_outcomes, "add_identity")  # after the insert, same tx
    linking = asyncio.create_task(
        sign_in_with(attacker, fake, "google", purpose="link", sub="in-flight")
    )
    await held.inside()
    resetting = asyncio.create_task(complete_reset(oenv, token))
    await asyncio.sleep(WAIT)
    assert not resetting.done()  # waiting for the user row the link holds
    held.release.set()
    assert (await linking).headers["location"] == LINKED
    assert await resetting == 200
    assert await oenv.scalar("SELECT count(*) FROM identities") == 0


# --- sign out everywhere and admin disable vs sign-in -------------------------------------------


async def test_sign_out_everywhere_waits_for_a_sign_in_in_flight(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await signed_in(env, EMAIL)  # TOTP verified just now: counts as step-up
    held = Held(monkeypatch, signin_routes, "remember_device")
    other = Browser(env)
    signing_in = asyncio.create_task(other.sign_in(EMAIL))
    await held.inside()
    csrf = await owner.csrf()
    out = asyncio.create_task(owner.post("/api/auth/logout-all", csrf=csrf))
    await asyncio.sleep(WAIT)
    assert not out.done()  # waiting for the user row the sign-in holds
    held.release.set()
    assert (await signing_in).status_code == 200
    assert (await out).status_code == 204
    assert await sessions(env) == 0


async def test_a_disable_between_the_password_check_and_the_session_wins(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await user_with_password(env)

    async def disable() -> None:
        await admin_accounts.disable_account(env.factory, user_id)

    after(monkeypatch, signin_routes, "_check_password", disable)
    assert (await Browser(env).sign_in(EMAIL)).status_code == 401
    assert await sessions(env) == 0


async def test_a_disable_waits_for_a_sign_in_in_flight_and_revokes_its_session(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await user_with_password(env)
    held = Held(monkeypatch, signin_routes, "remember_device")
    signing_in = asyncio.create_task(Browser(env).sign_in(EMAIL))
    await held.inside()
    disabling = asyncio.create_task(admin_accounts.disable_account(env.factory, user_id))
    await asyncio.sleep(WAIT)
    assert not disabling.done()
    held.release.set()
    assert (await signing_in).status_code == 200
    _, revoked = await disabling
    assert revoked == 1
    assert await sessions(env) == 0


# --- no deadlock --------------------------------------------------------------------------------


async def test_the_concurrent_pairs_never_deadlock(env: Env) -> None:
    """Each pair at once, a few rounds: every answer is a normal one (never a 500 from a
    deadlock or serialization failure), and no session outlives the reset or the disable."""
    user_id = await user_with_password(env)
    original = await env.scalar("SELECT password_hash FROM users")
    for _ in range(4):
        await env.run(
            "UPDATE users SET disabled_at = NULL, password_hash = :h WHERE id = :u",
            u=user_id,
            h=original,
        )
        token = await reset_token(env, user_id)
        signin, reset = await asyncio.gather(
            Browser(env).sign_in(EMAIL), complete_reset(env, token)
        )
        assert signin.status_code in (200, 401)
        assert reset == 200
        await env.run("DELETE FROM sessions")
        login, disabled = await asyncio.gather(
            Browser(env).sign_in(EMAIL),
            admin_accounts.disable_account(env.factory, user_id),
        )
        assert login.status_code in (200, 401)
        assert disabled[1] in (0, 1)
        assert await sessions(env) == 0
