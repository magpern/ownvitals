"""Sign-out events under the lock order of ``auth/userlock.py`` (#251, after #248).

Each sign-out takes the user row before its session row and writes its ``auth_events`` row
(whose foreign key takes ``FOR KEY SHARE`` on that user row) last. Without the lock first, a
sign-out holding its session row and an account deletion holding the user row wait for each
other: a deadlock. Each test fails if the lock is removed. The deletion is the admin delete on
its maint path since #314 (``racetools.AdminPath``): its audit row's ``FOR KEY SHARE`` on the
user row comes before the definer's lock. Synthetic accounts only, on the real database.
"""

import asyncio

import pytest
from sqlalchemy import text

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401 - fixtures
from flows import Browser, only_user_id, signed_in
from ownvitals.auth import routes
from racetools import ADMIN_EMAIL, EMAIL, WAIT, AdminPath, Held, admin_path


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
    async with admin_path(env) as admin:
        await admin.actor()
        signing_out = asyncio.create_task(browser.post("/api/auth/logout", csrf=csrf))
        await held.inside()
        deleting = asyncio.create_task(admin.delete(user_id, email=EMAIL))
        await asyncio.sleep(WAIT)
        assert not deleting.done()  # waiting for the user row the sign-out holds
        held.release.set()
        assert (await signing_out).status_code == 204
        await deleting  # no deadlock, no serialization error
    assert "sign_out" in await _kinds(env)
    assert await env.scalar("SELECT count(*) FROM users WHERE email <> :a", a=ADMIN_EMAIL) == 0


async def test_a_sign_out_waits_for_an_account_deletion_and_records_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = await signed_in(env, EMAIL)
    user_id = await only_user_id(env)
    csrf = await browser.csrf()  # before: reading the session waits for the deletion too
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:  # the user row is gone, uncommitted
        entered.set()
        await asyncio.wait_for(release.wait(), 10)

    async with admin_path(env) as admin:
        await admin.actor()
        deleting = asyncio.create_task(admin.delete(user_id, email=EMAIL, inside=hold))
        await asyncio.wait_for(entered.wait(), 10)
        signing_out = asyncio.create_task(browser.post("/api/auth/logout", csrf=csrf))
        await asyncio.sleep(WAIT)
        assert not signing_out.done()  # its transaction waits for the user row
        release.set()
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
    async with admin_path(env) as admin:
        await admin.actor()
        ending = asyncio.create_task(browser.request("DELETE", path, csrf=csrf))
        await held.inside()
        deleting = asyncio.create_task(admin.delete(user_id, email=EMAIL))
        await asyncio.sleep(WAIT)
        assert not deleting.done()
        held.release.set()
        assert (await ending).status_code == 204
        await deleting
    assert "session_signed_out" in await _kinds(env)


async def test_sign_outs_and_deletions_at_once_never_deadlock(env: Env) -> None:
    """Each pair at once, a few rounds: every answer is a normal one (never a 500 from a
    deadlock), and the account is gone afterwards."""
    async with admin_path(env) as admin:
        await admin.actor()
        await _rounds(env, admin)


async def _rounds(env: Env, admin: AdminPath) -> None:
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
            admin.delete(user_id, email=email),  # pyright: ignore[reportArgumentType]
            return_exceptions=True,
        )
        assert deleted is None
        # 204, or 403 when the deletion committed first (the CSRF token of a gone session)
        assert getattr(out, "status_code", None) in (204, 403)
        assert getattr(ended, "status_code", None) in (204, 403)
        assert await env.scalar("SELECT count(*) FROM users WHERE email = :e", e=email) == 0
