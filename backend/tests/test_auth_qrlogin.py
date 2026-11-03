"""QR approve-login: the flow, its deadlines, the bindings and the new session (ADR 0011 sections
5 to 7). Synthetic accounts, addresses and codes only; no real device is involved."""

import asyncio

import pytest
from sqlalchemy import text as sql

from authtools import (  # noqa: F401 - fixtures
    ADMIN_EMAIL,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    running_env,
    smtp_fixture,
)
from flows import Browser, reason, signed_in
from ownvitals.auth import accounts, purge
from ownvitals.auth.cookies import PRE_COOKIE, SESSION_COOKIE
from ownvitals.auth.useragent import describe
from passkeytools import BASE as PASSKEYS
from qrtools import (
    BASE,
    DESKTOP_IP,
    FIREFOX_ON_WINDOWS,
    QrBrowser,
    approve,
    approved,
    complete,
    fresh_code,
    nonce_of,
    phone,
    scan,
    start,
    unavailable,
)
from smtpfake import SmtpFake

EMAIL = "user@example.com"


async def session_answer(browser: Browser) -> dict:
    return (await browser.get("/api/auth/session")).json()


# --- the happy path and the new session --------------------------------------------------------


async def test_start_scan_approve_complete_gives_the_desktop_a_full_session(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env, user_agent=FIREFOX_ON_WINDOWS)
    started = await start(desktop)
    assert started["expires_in"] == 60
    assert started["qr_url"].startswith("https://ownvitals.test/qr#")
    assert started["qr_svg"].startswith("<svg")
    assert PRE_COOKIE in desktop.cookies
    scanned = await scan(mobile, started)
    assert scanned.status_code == 200, scanned.text
    shown = scanned.json()
    assert shown["id"] == started["id"]
    assert shown["desktop"] == {
        "browser": "Firefox",
        "os": "Windows",
        "ip": DESKTOP_IP,
        "place": "Place unknown",
    }
    assert shown["different_network"] is False
    assert 0 < shown["expires_in"] <= 120
    answer = await approve(mobile, started["id"], await fresh_code(env))
    assert (answer.status_code, answer.json()) == (200, {"status": "approved"})
    done = await complete(desktop, started["id"])
    assert done.status_code == 200, done.text
    assert done.json()["email"] == EMAIL
    assert desktop.signed_in
    assert PRE_COOKIE not in desktop.cookies
    answer = await session_answer(desktop)
    assert answer["user"]["email"] == EMAIL
    assert (
        answer["session"]["mfa"] is True
    )  # full: the code was entered on the phone for this approval
    row = await env.row(
        "SELECT reauth_at, stepup_at, mfa_at, origin, mfa_by_approval, approved_factor, "
        "approved_by_session_id FROM sessions WHERE origin = 'qr_approval'"
    )
    assert row.reauth_at is None
    assert row.stepup_at is None
    assert row.mfa_at is not None
    assert (row.origin, row.mfa_by_approval, row.approved_factor) == ("qr_approval", True, "totp")
    phone_session = await env.scalar("SELECT id FROM sessions WHERE origin = 'signin'")
    assert row.approved_by_session_id == phone_session
    assert await env.scalar("SELECT state FROM qr_logins") == "completed"
    assert await env.scalar("SELECT count(*) FROM pre_sessions") == 0
    # It works as a session: a data route answers.
    assert (await desktop.get("/api/auth/sessions")).status_code == 200


async def test_the_desktop_session_needs_its_own_proofs_and_cannot_approve(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    assert (await complete(desktop, qr_id)).status_code == 200
    # A step-up action needs its own TOTP on the desktop.
    answer = await desktop.post("/api/auth/logout-all")
    assert (answer.status_code, reason(answer)) == (403, "step_up_required")
    # No chaining: the QR-born session can't scan (and so can't approve) another QR login.
    other = QrBrowser(env, "198.51.100.9")
    started = await start(other)
    refused = await scan(desktop, started)
    assert (refused.status_code, reason(refused)) == (403, "qr_approval_not_allowed")
    assert await env.scalar("SELECT state FROM qr_logins WHERE id = :i", i=started["id"]) == (
        "pending"
    )
    # Its own TOTP later does not lift that: the origin stays.
    code = await fresh_code(env)
    assert (await desktop.post("/api/auth/mfa/stepup", {"code": code})).status_code == 200
    assert (await scan(desktop, started)).status_code == 403


async def test_the_admin_role_is_not_carried_by_the_approval(
    clean_auth: None, app_database_url: str
) -> None:
    async with running_env(app_database_url, admin_emails=ADMIN_EMAIL) as env:
        mobile = await phone(env, ADMIN_EMAIL.lower())
        assert (await session_answer(mobile))["user"]["role"] == "admin"
        desktop = QrBrowser(env)
        qr_id = await approved(env, desktop, mobile)
        assert (await complete(desktop, qr_id)).status_code == 200
        answer = await session_answer(desktop)
        assert answer["user"]["role"] == "user"  # the phone's code doesn't count for admin
        assert answer["session"]["admin_pending_mfa"] is True
        assert (await desktop.get("/api/admin/users")).status_code == 403
        # A TOTP verified in the desktop session itself does.
        code = await fresh_code(env)
        assert (await desktop.post("/api/auth/reauth/totp", {"code": code})).status_code == 200
        assert (await session_answer(desktop))["user"]["role"] == "admin"
        assert (
            await env.scalar("SELECT mfa_by_approval FROM sessions WHERE origin = 'qr_approval'")
            is False
        )


async def test_deny_ends_the_request(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    started = await start(desktop)
    assert (await scan(mobile, started)).status_code == 200
    denied = await mobile.post(f"{BASE}/{started['id']}/deny")
    assert (denied.status_code, denied.json()) == (200, {"status": "denied"})
    assert unavailable(await approve(mobile, started["id"], await fresh_code(env)))
    assert unavailable(await complete(desktop, started["id"]))
    assert unavailable(await mobile.post(f"{BASE}/{started['id']}/deny"))
    assert not desktop.signed_in
    kinds = await env.scalar(
        "SELECT array_agg(kind ORDER BY at) FROM auth_events WHERE kind LIKE 'qr%'"
    )
    assert kinds == [  # type: ignore[comparison-overlap]
        "qr_login_started",
        "qr_login_scanned",
        "qr_login_denied",
        "qr_login_refused",
        "qr_login_refused",
        "qr_login_refused",
    ]


# --- deadlines ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "works"), [("1 second", True), ("0 seconds", False), ("-1 second", False)]
)
async def test_the_scan_deadline_just_before_at_and_after(
    env: Env, offset: str, works: bool
) -> None:
    mobile = await phone(env)
    started = await start(QrBrowser(env))
    await env.run(
        "UPDATE qr_logins SET scan_deadline = now() + CAST(:offset AS interval)", offset=offset
    )
    answer = await scan(mobile, started)
    assert (answer.status_code == 200) is works
    if not works:
        assert unavailable(answer)


@pytest.mark.parametrize(
    ("offset", "works"), [("1 second", True), ("0 seconds", False), ("-1 second", False)]
)
async def test_the_approve_deadline_just_before_at_and_after(
    env: Env, offset: str, works: bool
) -> None:
    mobile = await phone(env)
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    await env.run(
        "UPDATE qr_logins SET approve_deadline = now() + CAST(:offset AS interval)", offset=offset
    )
    answer = await approve(mobile, started["id"], await fresh_code(env))
    assert (answer.status_code == 200) is works
    if not works:
        assert unavailable(answer)


@pytest.mark.parametrize(
    ("offset", "works"), [("1 second", True), ("0 seconds", False), ("-1 second", False)]
)
async def test_the_complete_deadline_just_before_at_and_after(
    env: Env, offset: str, works: bool
) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    await env.run(
        "UPDATE qr_logins SET complete_deadline = now() + CAST(:offset AS interval)", offset=offset
    )
    answer = await complete(desktop, qr_id)
    assert (answer.status_code == 200) is works
    if not works:
        assert unavailable(answer)
        assert await env.scalar("SELECT count(*) FROM sessions WHERE origin = 'qr_approval'") == 0


async def test_an_expired_code_is_refused_after_sixty_seconds(env: Env) -> None:
    mobile = await phone(env)
    started = await start(QrBrowser(env))
    deadline = await env.scalar("SELECT scan_deadline - created_at FROM qr_logins")
    assert deadline.total_seconds() == 60  # type: ignore[union-attr]
    await env.run(
        "UPDATE qr_logins SET created_at = now() - interval '61 seconds', "
        "scan_deadline = now() - interval '1 second'"
    )
    assert unavailable(await scan(mobile, started))


# --- bindings -----------------------------------------------------------------------------------


async def test_a_used_code_is_refused_to_any_session(env: Env) -> None:
    first = await phone(env)
    second = await phone(env, "other@example.com")
    started = await start(QrBrowser(env))
    assert (await scan(first, started)).status_code == 200
    assert unavailable(await scan(first, started))
    assert unavailable(await scan(second, started))  # the first scan wins
    # The second session can't approve or deny the first one's row either.
    assert unavailable(await approve(second, started["id"], await fresh_code(env)))
    assert unavailable(await second.post(f"{BASE}/{started['id']}/deny"))


async def test_complete_needs_the_pre_session_the_row_is_bound_to(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    stranger = QrBrowser(env, "203.0.113.50")
    assert unavailable(await complete(stranger, qr_id))  # no pre-session
    await start(stranger)  # a pre-session of its own
    stranger_own = await env.scalar("SELECT count(*) FROM qr_logins")
    assert stranger_own == 2
    assert unavailable(await complete(stranger, qr_id))  # another pre-session
    assert not stranger.signed_in
    assert (await complete(desktop, qr_id)).status_code == 200
    assert unavailable(await complete(desktop, qr_id))  # used


async def test_complete_before_approval_is_refused(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    started = await start(desktop)
    assert unavailable(await complete(desktop, started["id"]))
    assert (await scan(mobile, started)).status_code == 200
    assert unavailable(await complete(desktop, started["id"]))
    assert await env.scalar("SELECT count(*) FROM sessions WHERE origin = 'qr_approval'") == 0


async def test_scanning_needs_a_full_session(env: Env) -> None:
    started = await start(QrBrowser(env))
    anonymous = QrBrowser(env, "203.0.113.60")
    answer = await anonymous.post(f"{BASE}/scan", {"nonce": nonce_of(started)})
    assert answer.status_code == 401
    half = await signed_in(env, "half@example.com", second_factor=False)
    await env.enrol(await env.scalar("SELECT id FROM users WHERE email = 'half@example.com'"))  # type: ignore[arg-type]
    answer = await half.post(f"{BASE}/scan", {"nonce": nonce_of(started)})
    assert (answer.status_code, reason(answer)) == (403, "two_factor_required")
    assert await env.scalar("SELECT state FROM qr_logins") == "pending"


async def test_a_malformed_or_unknown_nonce_is_the_generic_answer(env: Env) -> None:
    mobile = await phone(env)
    await start(QrBrowser(env))
    for nonce in ("x", "A" * 43, "A" * 44, "!" * 43, "A" * 42 + "="):
        assert unavailable(await mobile.post(f"{BASE}/scan", {"nonce": nonce})), nonce
    assert await env.scalar("SELECT state FROM qr_logins") == "pending"


async def test_a_new_start_expires_the_previous_code(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    first = await start(desktop)
    second = await start(desktop)
    assert unavailable(await scan(mobile, first))
    assert (await scan(mobile, second)).status_code == 200
    third = await start(desktop)  # also expires a scanned row, which the approver is told of
    assert unavailable(await approve(mobile, second["id"], await fresh_code(env)))
    states = await env.scalar("SELECT array_agg(state ORDER BY created_at) FROM qr_logins")
    assert states == ["expired", "expired", "pending"]
    assert third["id"] != second["id"]
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'qr_login_expired'") == 1


async def test_a_passkey_sign_in_started_in_another_tab_keeps_the_qr_login(env: Env) -> None:
    """The #165 item (c): both tabs share the cookie jar; the passkey start gives a new token in
    the same pre-session, so the QR row stays bound and completes."""
    mobile = await phone(env)
    desktop = QrBrowser(env)
    started = await start(desktop)
    before = desktop.cookies[PRE_COOKIE]
    assert (await desktop.post(f"{PASSKEYS}/signin/options")).status_code == 200
    assert desktop.cookies[PRE_COOKIE] != before  # a new token...
    assert await env.scalar("SELECT count(*) FROM pre_sessions") == 1  # ...in the same row
    assert (await scan(mobile, started)).status_code == 200
    assert (await approve(mobile, started["id"], await fresh_code(env))).status_code == 200
    assert (await complete(desktop, started["id"])).status_code == 200
    # And the old token is dead.
    stale = QrBrowser(env)
    stale.cookies[PRE_COOKIE] = before
    assert unavailable(await complete(stale, started["id"]))


async def test_a_qr_start_keeps_a_passkey_sign_in_of_another_tab(env: Env) -> None:
    desktop = QrBrowser(env)
    assert (await desktop.post(f"{PASSKEYS}/signin/options")).status_code == 200
    await start(desktop)
    assert await env.scalar("SELECT count(*) FROM webauthn_challenges") == 1


async def test_disabled_qr_login_hides_start_and_refuses_scans(app_database_url: str) -> None:
    async with running_env(app_database_url, qr_login_enabled=False) as env:
        desktop = QrBrowser(env)
        answer = await desktop.post(f"{BASE}/start")
        assert (answer.status_code, reason(answer)) == (404, "not_found")
        mobile = await phone(env, "off@example.com")
        answer = await mobile.post(f"{BASE}/scan", {"nonce": "A" * 43})
        assert (answer.status_code, reason(answer)) == (404, "not_found")


# --- account state ------------------------------------------------------------------------------


async def test_a_revoked_approving_session_makes_complete_fail(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    await env.run("DELETE FROM sessions")  # log out everywhere, a password change
    assert unavailable(await complete(desktop, qr_id))
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert await env.scalar("SELECT state FROM qr_logins") == "approved"
    await start(desktop)  # the stream or the purge expires it
    assert await env.scalar("SELECT count(*) FROM qr_logins WHERE state = 'approved'") == 0


async def test_a_disabled_approver_makes_complete_fail(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    await env.run("UPDATE users SET disabled_at = now()")
    assert unavailable(await complete(desktop, qr_id))
    assert await env.scalar("SELECT count(*) FROM sessions WHERE origin = 'qr_approval'") == 0


async def test_mfa_reset_expires_the_accounts_live_rows(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    qr_id = await approved(env, desktop, mobile)
    other = QrBrowser(env, "198.51.100.20")
    second = await start(other)
    assert (await scan(mobile, second)).status_code == 200
    untouched = await start(QrBrowser(env, "198.51.100.21"))  # nobody scanned it
    async with env.db() as db:
        await accounts.reset_mfa(db, EMAIL)
    async with env.db() as db:
        rows = {
            str(r.id): r.state
            for r in (await db.execute(sql("SELECT id, state FROM qr_logins"))).all()
        }
    assert rows[qr_id] == "expired"
    assert rows[second["id"]] == "expired"
    assert rows[untouched["id"]] == "pending"
    assert unavailable(await complete(desktop, qr_id))
    assert await env.scalar("SELECT count(*) FROM sessions") == 0


# --- what the phone is shown --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("desktop_ip", "phone_ip", "different"),
    [
        ("198.51.100.7", "198.51.100.7", False),
        ("198.51.100.7", "203.0.113.7", True),
        ("2001:db8:1:2::10", "2001:db8:1:2::99", False),  # the same /64
        ("2001:db8:1:2::10", "2001:db8:1:3::10", True),
        ("198.51.100.7", "2001:db8:1:2::10", True),
    ],
)
async def test_the_different_network_flag(
    env: Env, desktop_ip: str, phone_ip: str, different: bool
) -> None:
    mobile = await phone(env, ip=phone_ip)
    started = await start(QrBrowser(env, desktop_ip))
    answer = await scan(mobile, started)
    assert answer.json()["different_network"] is different
    assert answer.json()["desktop"]["ip"] == desktop_ip


UA_CASES = [
    (FIREFOX_ON_WINDOWS, ("Firefox", "Windows")),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Safari/605.1.15",
        ("Safari", "macOS"),
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
        ("Edge", "Windows"),
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36",
        ("Chrome", "Linux"),
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
        "SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
        ("Samsung Internet", "Android"),
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) CriOS/126.0.6478.54 Mobile/15E148 Safari/604.1",
        ("Chrome", "iOS"),
    ),
    (
        "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Mobile/15E148 Safari/604.1",
        ("Safari", "iPadOS"),
    ),
    (
        "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 OPR/112.0.0.0",
        ("Opera", "ChromeOS"),
    ),
    ("curl/8.5.0", ("other", "other")),
    ("", ("other", "other")),
    (None, ("other", "other")),
]


@pytest.mark.parametrize(("agent", "expected"), UA_CASES)
def test_the_user_agent_parser(agent: str | None, expected: tuple[str, str]) -> None:
    assert describe(agent) == expected


async def test_the_raw_user_agent_is_not_stored(env: Env) -> None:
    marker = "Synthetic-Marker/9.9 " + FIREFOX_ON_WINDOWS
    mobile = await phone(env)
    desktop = QrBrowser(env, user_agent=marker)
    qr_id = await approved(env, desktop, mobile)
    dump = str(await env.scalar("SELECT to_jsonb(q)::text FROM qr_logins q WHERE id = :i", i=qr_id))
    assert "Synthetic-Marker" not in dump
    assert "Firefox" in dump


# --- the mail and the purge ---------------------------------------------------------------------


async def test_a_completed_approval_is_mailed(menv: Env, smtp: SmtpFake) -> None:
    mobile = await phone(menv)
    desktop = QrBrowser(menv, user_agent=FIREFOX_ON_WINDOWS)
    qr_id = await approved(menv, desktop, mobile)
    started_nonce = await menv.scalar("SELECT encode(nonce_hash, 'hex') FROM qr_logins")
    assert (await complete(desktop, qr_id)).status_code == 200
    for _ in range(100):
        if smtp.to(EMAIL):
            break
        await asyncio.sleep(0.05)
    [message] = smtp.to(EMAIL)
    text = message.text
    assert "approved from your phone" in message.subject
    assert "Firefox on Windows (Place unknown)" in text
    assert "https://ownvitals.test/settings/security" in text
    assert desktop.cookies[SESSION_COOKIE] not in text
    assert str(started_nonce) not in text


async def test_the_purge_expires_overdue_rows_and_deletes_them_an_hour_later(env: Env) -> None:
    mobile = await phone(env)
    started = await start(QrBrowser(env))
    assert (await scan(mobile, started)).status_code == 200
    stale = await start(QrBrowser(env, "198.51.100.30"))
    await env.run(
        "UPDATE qr_logins SET approve_deadline = now() - interval '1 second' WHERE id = :i",
        i=started["id"],
    )
    counts = await purge.purge_expired(env.factory, env.settings)
    assert counts["qr_logins_expired"] == 1
    assert counts["qr_logins"] == 0
    assert await env.scalar("SELECT state FROM qr_logins WHERE id = :i", i=started["id"]) == (
        "expired"
    )
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'qr_login_expired'") == 1
    await env.run(
        "UPDATE qr_logins SET scan_deadline = now() - interval '61 minutes', "
        "approve_deadline = now() - interval '61 minutes' WHERE id = :i",
        i=started["id"],
    )
    await env.run(
        "UPDATE qr_logins SET scan_deadline = now() - interval '59 minutes' WHERE id = :i",
        i=stale["id"],
    )
    counts = await purge.purge_expired(env.factory, env.settings)
    assert counts["qr_logins"] == 1  # an hour after its last deadline; the other is not yet
    assert counts["qr_logins_expired"] == 1  # the unscanned one, without an event
    assert await env.scalar("SELECT count(*) FROM qr_logins") == 1
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'qr_login_expired'") == 1
