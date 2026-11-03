"""QR approve-login races (ADR 0011 sections 3 and 5; the #165 lock-order item for ADR 0011).

Each pair runs concurrently through the real routes; exactly one outcome may win. ``complete``
and ``admin mfa-reset`` follow the passkeys' lock order (the account row first): whichever
commits first, no live session is left. Synthetic accounts and codes only."""

import asyncio

from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from ownvitals.auth import accounts
from ownvitals.auth.passkeys import lock_user
from qrtools import (
    BASE,
    QrBrowser,
    approve,
    approved,
    fresh_code,
    phone,
    recovery_codes,
    scan,
    start,
    unavailable,
)

EMAIL = "user@example.com"


async def test_two_concurrent_scans_one_wins(env: Env) -> None:
    first = await phone(env)
    second = await phone(env, "other@example.com")
    started = await start(QrBrowser(env))
    answers = await asyncio.gather(scan(first, started), scan(second, started))
    assert sorted(a.status_code for a in answers) == [200, 404]
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'qr_login_scanned'") == 1


async def test_two_concurrent_approvals_one_wins(env: Env) -> None:
    """Both codes are valid (a TOTP and a recovery code), so only the row decides."""
    mobile = await phone(env)
    user_id = await env.scalar("SELECT id FROM users")
    recovery = (await recovery_codes(env, user_id))[0]  # type: ignore[arg-type]
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    answers = await asyncio.gather(
        approve(mobile, started["id"], await fresh_code(env)),
        approve(mobile, started["id"], recovery),
    )
    assert sorted(a.status_code for a in answers) == [200, 404]
    assert (
        await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'qr_login_approved'") == 1
    )


async def test_approve_and_deny_concurrently_one_outcome(env: Env) -> None:
    mobile = await phone(env)
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    code = await fresh_code(env)
    answers = await asyncio.gather(
        approve(mobile, started["id"], code), mobile.post(f"{BASE}/{started['id']}/deny")
    )
    assert sorted(a.status_code for a in answers) == [200, 404]
    state = await env.scalar("SELECT state FROM qr_logins")
    assert state in ("approved", "denied")
    kinds = await env.scalar(
        "SELECT count(*) FROM auth_events WHERE kind IN ('qr_login_approved', 'qr_login_denied')"
    )
    assert kinds == 1


async def test_two_concurrent_completes_give_one_session(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    twin = QrBrowser(env)  # the same browser, two tabs
    twin.cookies = dict(desktop.cookies)
    csrf = await desktop.csrf()
    answers = await asyncio.gather(
        desktop.post(f"{BASE}/{qr_id}/complete", csrf=csrf),
        twin.post(f"{BASE}/{qr_id}/complete", csrf=csrf),
    )
    assert sorted(a.status_code for a in answers) == [200, 404]
    assert await env.scalar("SELECT count(*) FROM sessions WHERE origin = 'qr_approval'") == 1


async def test_approve_after_expiry_is_refused(env: Env) -> None:
    mobile = await phone(env)
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    await env.run("UPDATE qr_logins SET approve_deadline = now()")
    assert unavailable(await approve(mobile, started["id"], await fresh_code(env)))


async def test_a_complete_waiting_for_a_reset_finds_its_row_expired(env: Env) -> None:
    """The reset holds the account row; the complete waits for it, then finds nothing."""
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    csrf = await desktop.csrf()
    async with env.db() as db:
        await accounts.reset_mfa(db, EMAIL)  # holds the lock until this block commits
        attempt = asyncio.create_task(desktop.post(f"{BASE}/{qr_id}/complete", csrf=csrf))
        await asyncio.sleep(0.5)
        assert not attempt.done()  # waiting for the account row
    assert unavailable(await attempt)
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert await env.scalar("SELECT state FROM qr_logins") == "expired"


async def test_a_complete_committing_just_before_a_reset_leaves_no_live_session(env: Env) -> None:
    """The other order: the complete holds the account row (``FOR SHARE``) and commits its
    session; the reset waits for it, then revokes that session with the rest."""
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    user_id = await env.scalar("SELECT id FROM users")
    csrf = await desktop.csrf()
    async with env.db() as holder:
        await holder.execute(text("SELECT 1 FROM users FOR NO KEY UPDATE"))
        attempt = asyncio.create_task(desktop.post(f"{BASE}/{qr_id}/complete", csrf=csrf))
        await asyncio.sleep(0.5)
        assert not attempt.done()  # complete takes the account row before its final write
        assert await env.scalar("SELECT count(*) FROM sessions WHERE origin = 'qr_approval'") == 0
    assert (await attempt).status_code == 200

    async def reset() -> None:
        async with env.db() as db:
            await accounts.reset_mfa(db, EMAIL)

    async with env.db() as holder:
        await lock_user(holder, user_id, share=True)  # pyright: ignore[reportArgumentType]
        task = asyncio.create_task(reset())
        await asyncio.sleep(0.5)
        assert not task.done()  # the reset waits for the account row too
    await task
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert (await desktop.get("/api/auth/sessions")).status_code == 401
