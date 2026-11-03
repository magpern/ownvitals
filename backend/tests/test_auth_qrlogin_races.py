"""QR approve-login races (ADR 0011 sections 3 and 5; the #165 lock-order item for ADR 0011).

Each pair runs concurrently through the real routes; exactly one outcome may win. Where the
read-only pre-check would otherwise settle the race, the requests are held after it, inside their
transactions (``held_after``, or an event), so the conditional ``UPDATE`` is what decides.
``complete`` and ``admin mfa-reset`` follow the passkeys' lock order (the account row first):
whichever commits first, no live session is left. Synthetic accounts and codes only."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from ownvitals.auth import accounts, mfa, qrlogin
from ownvitals.auth.cookies import SESSION_COOKIE
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


def held_after(
    monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, parties: int = 2
) -> asyncio.Barrier:
    """Patch ``owner.name`` so that each caller, after the real call, waits until ``parties``
    callers got there. Every request is then past its read-only pre-check and inside its own
    transaction, and only the conditional ``UPDATE`` that follows can decide between them."""
    barrier = asyncio.Barrier(parties)
    real: Callable[..., Awaitable[Any]] = getattr(owner, name)

    async def held(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        await asyncio.wait_for(barrier.wait(), 10)
        return result

    monkeypatch.setattr(owner, name, held)
    return barrier


async def count(env: Env, kind: str) -> object:
    return await env.scalar("SELECT count(*) FROM auth_events WHERE kind = :k", k=kind)


async def test_two_concurrent_scans_one_wins(env: Env) -> None:
    first = await phone(env)
    second = await phone(env, "other@example.com")
    started = await start(QrBrowser(env))
    answers = await asyncio.gather(scan(first, started), scan(second, started))
    assert sorted(a.status_code for a in answers) == [200, 404]
    assert await count(env, "qr_login_scanned") == 1


async def test_two_concurrent_approvals_one_wins(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both codes are valid (a TOTP and a recovery code), and both requests are held after the
    pre-check and the code check until the other got there too: only the conditional
    ``UPDATE`` decides."""
    mobile = await phone(env)
    user_id = await env.scalar("SELECT id FROM users")
    recovery = (await recovery_codes(env, user_id))[0]  # type: ignore[arg-type]
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    code = await fresh_code(env)
    csrf = await mobile.csrf()
    held_after(monkeypatch, mfa, "check_second_factor")
    answers = await asyncio.gather(
        mobile.post(f"{BASE}/{started['id']}/approve", {"code": code}, csrf=csrf),
        mobile.post(f"{BASE}/{started['id']}/approve", {"code": recovery}, csrf=csrf),
    )
    assert sorted(a.status_code for a in answers) == [200, 404]
    assert await count(env, "qr_login_approved") == 1
    assert await env.scalar("SELECT state FROM qr_logins") == "approved"


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


async def test_a_deny_landing_between_the_code_check_and_the_update_wins(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval is held after its pre-check and code check until a deny has committed: its
    conditional ``UPDATE`` then finds the row denied."""
    mobile = await phone(env)
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    code = await fresh_code(env)
    csrf = await mobile.csrf()
    checked, denied = asyncio.Event(), asyncio.Event()
    real = mfa.check_second_factor

    async def held(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        checked.set()
        await asyncio.wait_for(denied.wait(), 10)
        return result

    monkeypatch.setattr(mfa, "check_second_factor", held)
    approval = asyncio.create_task(
        mobile.post(f"{BASE}/{started['id']}/approve", {"code": code}, csrf=csrf)
    )
    await asyncio.wait_for(checked.wait(), 10)
    deny = await mobile.post(f"{BASE}/{started['id']}/deny", csrf=csrf)
    denied.set()
    assert deny.status_code == 200
    assert unavailable(await approval)
    assert await env.scalar("SELECT state FROM qr_logins") == "denied"
    assert await count(env, "qr_login_approved") == 0


async def test_two_concurrent_completes_give_one_session(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both requests pass the pre-check and hold the account row (``FOR SHARE``) before either
    updates the row: only the conditional ``UPDATE`` decides."""
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    twin = QrBrowser(env)  # the same browser, two tabs
    twin.cookies = dict(desktop.cookies)
    csrf = await desktop.csrf()
    held_after(monkeypatch, qrlogin, "lock_user")
    answers = await asyncio.gather(
        desktop.post(f"{BASE}/{qr_id}/complete", csrf=csrf),
        twin.post(f"{BASE}/{qr_id}/complete", csrf=csrf),
    )
    assert sorted(a.status_code for a in answers) == [200, 404]
    assert await env.scalar("SELECT count(*) FROM sessions WHERE origin = 'qr_approval'") == 1
    assert await count(env, "qr_login_completed") == 1


async def test_a_new_code_landing_between_the_pre_check_and_the_update_wins(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complete is held after its pre-check and the account-row lock while another tab of
    the same browser starts a new code, which expires the approved row (the pre-session stays
    the same row, so the binding still matches): only the ``state`` condition of complete's
    ``UPDATE`` can refuse it."""
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    csrf = await desktop.csrf()
    locked, started = asyncio.Event(), asyncio.Event()
    real = qrlogin.lock_user

    async def held(*args: Any, **kwargs: Any) -> Any:
        result = await real(*args, **kwargs)
        locked.set()
        await asyncio.wait_for(started.wait(), 10)
        return result

    monkeypatch.setattr(qrlogin, "lock_user", held)
    attempt = asyncio.create_task(desktop.post(f"{BASE}/{qr_id}/complete", csrf=csrf))
    await asyncio.wait_for(locked.wait(), 10)
    other_tab = QrBrowser(env)
    other_tab.cookies = dict(desktop.cookies)
    await start(other_tab)
    started.set()
    assert unavailable(await attempt)
    assert await env.scalar("SELECT state FROM qr_logins WHERE id = :i", i=qr_id) == "expired"
    assert await env.scalar("SELECT count(*) FROM sessions WHERE origin = 'qr_approval'") == 0


async def test_only_the_scanning_session_may_approve_or_deny(env: Env) -> None:
    """A second full session of the same account is refused both (ADR 0011 section 5 step 4)."""
    mobile = await phone(env)
    user_id = await env.scalar("SELECT id FROM users")
    other_session = await env.full_login(user_id)  # pyright: ignore[reportArgumentType]
    other = QrBrowser(env)
    other.cookies[SESSION_COOKIE] = other_session.token
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    assert unavailable(await approve(other, started["id"], await fresh_code(env)))
    assert unavailable(await other.post(f"{BASE}/{started['id']}/deny"))
    assert await env.scalar("SELECT state FROM qr_logins") == "scanned"
    assert (await approve(mobile, started["id"], await fresh_code(env))).status_code == 200


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
