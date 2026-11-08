"""Sign-out events under the lock order of ``auth/userlock.py`` (#251, after #248).

Each sign-out takes the user row before its session row and writes its ``auth_events`` row
(whose foreign key takes ``FOR KEY SHARE`` on that user row) last. Without the lock first, a
sign-out holding its session row and an account deletion holding the user row wait for each
other: a deadlock. Each test fails if the lock is removed. Synthetic accounts only, on the real
database.
"""

import asyncio

import pytest
from sqlalchemy import text

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401 - fixtures
from flows import Browser, only_user_id, signed_in
from ownvitals.auth import admin_accounts, routes
from racetools import EMAIL, WAIT, Held


async def _kinds(env: Env) -> list[object]:
    async with env.db() as db:
        rows = await db.execute(text("SELECT kind FROM auth_events ORDER BY at, id"))
        return [row.kind for row in rows.all()]


async def test_an_account_deletion_waits_for_a_sign_out_in_flight(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = await signed_in(env, EMAIL)
    user_id = await only_user_id(env)
    held = Held(monkeypatch, routes, "revoke_session")  # the session row is deleted, uncommitted
    csrf = await browser.csrf()
    signing_out = asyncio.create_task(browser.post("/api/auth/logout", csrf=csrf))
    await held.inside()
    deleting = asyncio.create_task(
        admin_accounts.delete_account(env.factory, user_id, email=EMAIL, ip=None)
    )
    await asyncio.sleep(WAIT)
    assert not deleting.done()  # waiting for the user row the sign-out holds
    held.release.set()
    assert (await signing_out).status_code == 204
    await deleting  # no deadlock, no serialization error
    assert "sign_out" in await _kinds(env)
    assert await env.scalar("SELECT count(*) FROM users") == 0


async def test_a_sign_out_waits_for_an_account_deletion_and_records_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = await signed_in(env, EMAIL)
    user_id = await only_user_id(env)
    csrf = await browser.csrf()  # before: reading the session waits for the deletion too
    held = Held(monkeypatch, admin_accounts, "delete_user_account")  # the user row is gone
    deleting = asyncio.create_task(
        admin_accounts.delete_account(env.factory, user_id, email=EMAIL, ip=None)
    )
    await held.inside()
    signing_out = asyncio.create_task(browser.post("/api/auth/logout", csrf=csrf))
    await asyncio.sleep(WAIT)
    assert not signing_out.done()  # its transaction waits for the user row
    held.release.set()
    await deleting
    assert (await signing_out).status_code == 204
    assert "sign_out" not in await _kinds(env)


async def test_ending_another_session_waits_for_an_account_deletion(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = await signed_in(env, EMAIL)
    user_id = await only_user_id(env)
    spare = await env.login(user_id)
    csrf = await browser.csrf()
    held = Held(monkeypatch, routes, "revoke_session")
    path = f"/api/auth/sessions/{spare.id}"
    ending = asyncio.create_task(browser.request("DELETE", path, csrf=csrf))
    await held.inside()
    deleting = asyncio.create_task(
        admin_accounts.delete_account(env.factory, user_id, email=EMAIL, ip=None)
    )
    await asyncio.sleep(WAIT)
    assert not deleting.done()
    held.release.set()
    assert (await ending).status_code == 204
    await deleting
    assert "session_signed_out" in await _kinds(env)


async def test_sign_outs_and_deletions_at_once_never_deadlock(env: Env) -> None:
    """Each pair at once, a few rounds: every answer is a normal one (never a 500 from a
    deadlock), and the account is gone afterwards."""
    for round_ in range(4):
        email = f"round{round_}@example.com"
        browser = await signed_in(env, email)
        user_id = await env.scalar("SELECT id FROM users WHERE email = :e", e=email)
        spare = Browser(env)
        assert (await spare.sign_in(email)).status_code == 200
        csrf = await browser.csrf()
        out, ended, deleted = await asyncio.gather(
            browser.post("/api/auth/logout", csrf=csrf),
            spare.post("/api/auth/logout"),
            admin_accounts.delete_account(
                env.factory,
                user_id,  # pyright: ignore[reportArgumentType]
                email=email,
                ip=None,
            ),
            return_exceptions=True,
        )
        assert deleted is None
        # 204, or 403 when the deletion committed first (the CSRF token of a gone session)
        assert getattr(out, "status_code", None) in (204, 403)
        assert getattr(ended, "status_code", None) in (204, 403)
        assert await env.scalar("SELECT count(*) FROM users WHERE email = :e", e=email) == 0
