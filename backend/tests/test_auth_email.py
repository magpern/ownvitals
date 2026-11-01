"""Email change and account deletion (ADR 0003 sections 5 and 6)."""

import logging

import pytest

from authtools import Env
from flows import Browser, age_reauth, only_user_id, reason, signed_in, user_with_password
from smtpfake import SmtpFake

EMAIL = "user@example.com"
NEW = "new.address@example.com"


async def change(browser: Browser, new_email: str = NEW):
    return await browser.post("/api/auth/email/change", {"new_email": new_email})


async def confirm(browser: Browser, token: str):
    return await browser.post("/api/auth/email/confirm", {"token": token})


async def test_the_request_needs_recent_authentication(menv: Env) -> None:
    browser = await signed_in(menv)
    await age_reauth(menv)
    refused = await change(browser)
    assert (refused.status_code, reason(refused)) == (403, "reauth_required")
    assert (await change(Browser(menv))).status_code == 401


async def test_a_free_and_a_taken_address_get_the_same_answer_and_different_mails(
    menv: Env, smtp: SmtpFake
) -> None:
    browser = await signed_in(menv)
    await menv.user("taken@example.com")
    free = await change(browser)
    taken = await change(browser, "taken@example.com")
    assert (free.status_code, free.json()) == (taken.status_code, taken.json())
    [confirm_mail] = smtp.to(NEW)
    assert "#token=" in confirm_mail.text
    assert "/email-change#token=" in confirm_mail.text
    [taken_mail] = smtp.to("taken@example.com")
    assert "#token=" not in taken_mail.text
    assert "already" in taken_mail.text
    assert await menv.scalar("SELECT count(*) FROM auth_tokens") == 1  # none for the taken one
    assert (
        await menv.scalar("SELECT email FROM users WHERE email_normalized = :e", e=EMAIL) == EMAIL
    )


async def test_the_request_rejects_a_malformed_address(menv: Env) -> None:
    browser = await signed_in(menv)
    for bad in (
        "nope",
        "a@b",
        "a b@example.com",
        "a@@example.com",
        "x@example.com\r\nBcc: y@z.org",
    ):
        assert (await change(browser, bad)).status_code == 422


async def test_email_change_mail_is_limited_per_address(menv: Env, smtp: SmtpFake) -> None:
    browser = await signed_in(menv)
    codes = [(await change(browser)).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 429]


async def test_confirming_from_the_requesting_session_changes_the_email(
    menv: Env, smtp: SmtpFake
) -> None:
    browser = await signed_in(menv)
    user_id = await only_user_id(menv)
    await menv.login(user_id)  # a second session
    await change(browser)
    token = smtp.to(NEW)[0].token()
    response = await confirm(browser, token)
    assert (response.status_code, response.json()["email"]) == (200, NEW)
    assert await menv.scalar("SELECT email FROM users") == NEW
    assert await menv.scalar("SELECT email_normalized FROM users") == NEW
    assert await menv.scalar("SELECT count(*) FROM sessions") == 1  # every other session revoked
    assert await menv.scalar("SELECT count(*) FROM auth_tokens") == 0
    [notice] = smtp.to(EMAIL)  # the old address is notified
    assert "changed" in notice.text
    again = await confirm(browser, token)
    assert (again.status_code, reason(again)) == (400, "invalid_token")
    assert (await Browser(menv).sign_in(NEW)).status_code == 200


async def test_another_users_session_cannot_confirm(menv: Env, smtp: SmtpFake) -> None:
    owner = await signed_in(menv)
    attacker = await signed_in(menv, "attacker@example.com")
    await change(owner)
    token = smtp.to(NEW)[0].token()
    refused = await confirm(attacker, token)
    assert (refused.status_code, reason(refused)) == (400, "invalid_token")  # as for unknown
    unknown = await confirm(attacker, "no-such-token")
    assert refused.json() == unknown.json()
    assert (
        await menv.scalar("SELECT email FROM users WHERE email_normalized = :e", e=EMAIL) == EMAIL
    )
    assert await menv.scalar("SELECT count(*) FROM auth_tokens WHERE used_at IS NULL") == 1
    assert (await confirm(owner, token)).status_code == 200  # the token was left untouched


async def test_confirming_without_a_session_is_401(menv: Env, smtp: SmtpFake) -> None:
    browser = await signed_in(menv)
    await change(browser)
    anonymous = await confirm(Browser(menv), smtp.to(NEW)[0].token())
    assert anonymous.status_code == 401
    assert await menv.scalar("SELECT count(*) FROM auth_tokens WHERE used_at IS NULL") == 1


async def test_an_expired_token_is_refused(menv: Env, smtp: SmtpFake) -> None:
    browser = await signed_in(menv)
    await change(browser)
    await menv.run("UPDATE auth_tokens SET expires_at = now() - interval '1 second'")
    refused = await confirm(browser, smtp.to(NEW)[0].token())
    assert (refused.status_code, reason(refused)) == (400, "invalid_token")


async def test_an_address_taken_meanwhile_is_refused_and_not_logged(
    menv: Env, smtp: SmtpFake, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    browser = await signed_in(menv)
    await change(browser)
    await menv.user(NEW.upper())
    refused = await confirm(browser, smtp.to(NEW)[0].token())
    assert (refused.status_code, reason(refused)) == (409, "email_taken")
    assert (
        await menv.scalar("SELECT email FROM users WHERE email_normalized = :e", e=EMAIL) == EMAIL
    )
    assert await menv.scalar("SELECT count(*) FROM auth_tokens WHERE used_at IS NULL") == 1
    assert NEW.lower() not in caplog.text.lower()


async def test_a_changed_address_since_the_request_is_refused(menv: Env, smtp: SmtpFake) -> None:
    browser = await signed_in(menv)
    await change(browser)
    await menv.run("UPDATE users SET email = 'moved@example.com'")
    refused = await confirm(browser, smtp.to(NEW)[0].token())
    assert (refused.status_code, reason(refused)) == (400, "invalid_token")


async def test_deleting_the_account_needs_recent_auth_and_removes_everything(menv: Env) -> None:
    browser = await signed_in(menv)
    user_id = await only_user_id(menv)
    await menv.run(
        "INSERT INTO identities (user_id, provider, subject) VALUES (:u, 'github', 'x')", u=user_id
    )
    await age_reauth(menv)
    refused = await browser.request("DELETE", "/api/auth/account")
    assert (refused.status_code, reason(refused)) == (403, "reauth_required")
    assert await menv.scalar("SELECT count(*) FROM users") == 1
    await browser.post("/api/auth/reauth", {"password": "correct horse battery staple"})
    gone = await browser.request("DELETE", "/api/auth/account")
    assert gone.status_code == 204
    for table in ("users", "sessions", "identities", "known_devices"):
        assert await menv.scalar(f"SELECT count(*) FROM {table}") == 0  # noqa: S608
    assert not browser.signed_in
    assert await menv.scalar("SELECT count(*) FROM auth_events WHERE kind = 'account_deleted'") == 1
    assert (
        await menv.scalar("SELECT user_id FROM auth_events WHERE kind = 'account_deleted'") is None
    )


async def test_deleting_the_account_needs_step_up_when_totp_is_confirmed(menv: Env) -> None:
    browser = await signed_in(menv)
    await menv.run(
        "INSERT INTO mfa_totp (user_id, secret_ciphertext, nonce, key_id, confirmed_at) "
        "SELECT id, 'x', 'y', 'app-v1', now() FROM users"
    )
    refused = await browser.request("DELETE", "/api/auth/account")
    assert (refused.status_code, reason(refused)) == (403, "stepup_required")
    await menv.run("UPDATE sessions SET stepup_at = now()")
    assert (await browser.request("DELETE", "/api/auth/account")).status_code == 204


async def test_deleting_leaves_other_users_alone(menv: Env) -> None:
    browser = await signed_in(menv)
    await user_with_password(menv, "other@example.com")
    assert (await browser.request("DELETE", "/api/auth/account")).status_code == 204
    assert await menv.scalar("SELECT email FROM users") == "other@example.com"


async def test_anonymous_deletion_is_401(menv: Env) -> None:
    assert (await Browser(menv).request("DELETE", "/api/auth/account")).status_code == 401


async def test_email_change_requests_also_count_per_account_and_record_the_ip(menv: Env) -> None:
    from ownvitals.auth.ratelimit import MAIL_ACCOUNT

    browser = await signed_in(menv)
    user_id = await only_user_id(menv)
    assert (await change(browser)).status_code == 202
    digest = menv.app.state.limiter.bucket_hash(MAIL_ACCOUNT, f"user:{user_id}")
    assert await menv.scalar("SELECT count FROM rate_limits WHERE bucket_hash = :h", h=digest) == 1
    ip = await menv.scalar("SELECT ip FROM auth_events WHERE kind = 'email_change_requested'")
    assert ip == "127.0.0.1"
