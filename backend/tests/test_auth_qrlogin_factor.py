"""The QR approval's own code, and the QR rate limits (ADR 0011 sections 5, 7 and 8).

Approve needs a TOTP or recovery code entered **with the approval**: a step-up made earlier in the
10-minute window does not count, the approval does not set the phone's ``stepup_at``, a TOTP step
counts once, a recovery code works once, and a wrong code is an ``mfa_failed``. Synthetic
accounts and codes only."""

import pytest

from authtools import (  # noqa: F401 - fixtures
    TEST_TOTP_SECRET,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    running_env,
)
from flows import reason
from ownvitals.auth import totp
from qrtools import (
    BASE,
    QrBrowser,
    approve,
    fresh_code,
    phone,
    recovery_codes,
    scan,
    start,
    unavailable,
)
from windowtools import clear_of_window


async def scanned(env: Env, mobile: QrBrowser) -> str:
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    return started["id"]


async def test_approve_without_a_code_is_refused_even_after_a_fresh_step_up(env: Env) -> None:
    mobile = await phone(env)
    qr_id = await scanned(env, mobile)
    await env.run("UPDATE sessions SET stepup_at = now() - interval '1 minute'")
    for body in ({}, {"code": ""}, None):
        answer = await mobile.post(f"{BASE}/{qr_id}/approve", body)
        assert answer.status_code == 422, body
    assert await env.scalar("SELECT state FROM qr_logins") == "scanned"


async def test_a_wrong_code_is_refused_and_counted(env: Env) -> None:
    mobile = await phone(env)
    qr_id = await scanned(env, mobile)
    answer = await approve(mobile, qr_id, "000000")
    assert (answer.status_code, reason(answer)) == (400, "invalid_code")
    detail = await env.scalar("SELECT detail FROM auth_events WHERE kind = 'mfa_failed'")
    assert detail == {"purpose": "qr_approve"}
    assert await env.scalar("SELECT state FROM qr_logins") == "scanned"
    assert (await approve(mobile, qr_id, await fresh_code(env))).status_code == 200


async def test_a_replayed_totp_step_is_refused(env: Env) -> None:
    mobile = await phone(env)
    qr_id = await scanned(env, mobile)
    # The phone's own sign-in used the current step: the same code can't approve.
    used = totp.code_for_step(TEST_TOTP_SECRET, int(totp.clock() // totp.PERIOD))
    answer = await approve(mobile, qr_id, used)
    assert (answer.status_code, reason(answer)) == (400, "invalid_code")
    assert await env.scalar("SELECT state FROM qr_logins") == "scanned"


async def test_a_recovery_code_works_once(env: Env) -> None:
    mobile = await phone(env)
    user_id = await env.scalar("SELECT id FROM users")
    code = (await recovery_codes(env, user_id))[0]  # type: ignore[arg-type]
    first = await scanned(env, mobile)
    assert (await approve(mobile, first, code)).status_code == 200
    assert await env.scalar("SELECT approved_factor FROM qr_logins WHERE id = :i", i=first) == (
        "recovery_code"
    )
    second = await scanned(env, mobile)
    answer = await approve(mobile, second, code)
    assert (answer.status_code, reason(answer)) == (400, "invalid_code")


async def test_the_phones_step_up_is_not_set_by_an_approval(env: Env) -> None:
    mobile = await phone(env)
    await env.run("UPDATE sessions SET stepup_at = NULL")
    before = await env.row("SELECT id, stepup_at, mfa_at, reauth_at FROM sessions")
    qr_id = await scanned(env, mobile)
    assert (await approve(mobile, qr_id, await fresh_code(env))).status_code == 200
    after = await env.row("SELECT id, stepup_at, mfa_at, reauth_at FROM sessions")
    assert after == before  # not set, and not rotated
    answer = await mobile.post("/api/auth/logout-all")
    assert (answer.status_code, reason(answer)) == (403, "step_up_required")


async def test_no_code_is_checked_for_a_row_that_cannot_be_approved(env: Env) -> None:
    mobile = await phone(env)
    qr_id = await scanned(env, mobile)
    await env.run("UPDATE qr_logins SET state = 'denied'")
    code = await fresh_code(env)
    assert unavailable(await approve(mobile, qr_id, code))
    assert await env.scalar("SELECT last_step FROM mfa_totp") == 0  # the code was not spent
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'mfa_failed'") == 0


# --- rate limits ----------------------------------------------------------------------------------


async def test_approvals_are_limited_to_three_an_hour_per_account(env: Env) -> None:
    await clear_of_window(3600)
    mobile = await phone(env)
    for _ in range(3):
        qr_id = await scanned(env, mobile)
        assert (await approve(mobile, qr_id, await fresh_code(env))).status_code == 200
    qr_id = await scanned(env, mobile)
    answer = await approve(mobile, qr_id, await fresh_code(env))
    assert answer.status_code == 429
    assert int(answer.headers["retry-after"]) > 0
    assert await env.scalar("SELECT state FROM qr_logins WHERE id = :i", i=qr_id) == "scanned"


async def test_scans_are_limited_to_ten_in_ten_minutes_per_account(env: Env) -> None:
    await clear_of_window(600)
    mobile = await phone(env)
    for _ in range(10):
        answer = await mobile.post(f"{BASE}/scan", {"nonce": "A" * 43})
        assert unavailable(answer)
    answer = await mobile.post(f"{BASE}/scan", {"nonce": "A" * 43})
    assert answer.status_code == 429
    other = await phone(env, "other@example.com")  # another account is not affected
    assert unavailable(await other.post(f"{BASE}/scan", {"nonce": "A" * 43}))


@pytest.mark.parametrize("family", ["ipv4", "ipv6"])
async def test_starts_are_limited_to_twenty_in_ten_minutes_per_ip(env: Env, family: str) -> None:
    await clear_of_window(600)
    first, neighbour, other = (
        ("198.51.100.40", "198.51.100.40", "198.51.100.41")
        if family == "ipv4"
        else ("2001:db8:5:6::1", "2001:db8:5:6::ffff", "2001:db8:5:7::1")  # /64 buckets
    )
    for i in range(20):
        ip = first if i % 2 else neighbour
        assert (await QrBrowser(env, ip).post(f"{BASE}/start")).status_code == 200
    assert (await QrBrowser(env, first).post(f"{BASE}/start")).status_code == 429
    assert (await QrBrowser(env, other).post(f"{BASE}/start")).status_code == 200


async def test_the_start_limit_key_is_not_spoofable(
    clean_auth: None, app_database_url: str
) -> None:
    """With no trusted proxy, ``X-Forwarded-For`` and ``CF-Connecting-IP`` are ignored and the
    bucket is the peer (ADR 0003 section 8, the #165 item b): changing them changes nothing."""
    await clear_of_window(600)
    async with running_env(app_database_url, trusted_proxies="") as env:
        for i in range(21):
            answer = await env.client.post(
                f"{BASE}/start",
                headers={
                    "Origin": "https://ownvitals.test",
                    "X-CSRF-Token": "synthetic-csrf",
                    "Cookie": "__Host-ownvitals_csrf=synthetic-csrf",
                    "X-Forwarded-For": f"192.0.2.{i}",
                    "CF-Connecting-IP": f"192.0.2.{100 + i}",
                },
            )
            assert answer.status_code == (200 if i < 20 else 429), (i, answer.text)
