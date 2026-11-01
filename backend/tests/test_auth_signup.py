"""Sign-up through pending sign-ups and its confirmation (ADR 0003 section 5)."""

import logging
from datetime import timedelta

import pytest
from pydantic import ValidationError

from authtools import (  # noqa: F401 - fixtures
    ORIGIN,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    running_env,
    smtp_fixture,
)
from flows import PASSWORD, Browser, reason
from ownvitals.auth.accounts import create_invite
from smtpfake import SmtpFake

EMAIL = "new.user@example.com"


async def invite(env: Env) -> str:
    async with env.db() as db:
        return await create_invite(db, timedelta(days=1))


async def sign_up(browser: Browser, code: str | None, email: str = EMAIL, password: str = PASSWORD):
    return await browser.post(
        "/api/auth/signup", {"email": email, "password": password, "invite_code": code}
    )


async def test_a_new_address_gets_a_pending_row_and_one_confirmation_mail(
    menv: Env, smtp: SmtpFake
) -> None:
    code = await invite(menv)
    response = await sign_up(Browser(menv), code)
    assert (response.status_code, response.json()) == (202, {"status": "check_your_inbox"})
    [mail] = smtp.to(EMAIL)
    assert f"{ORIGIN}/signup/confirm#token=" in mail.text
    token = mail.token()
    assert await menv.scalar("SELECT count(*) FROM users") == 0  # no unverified account
    assert await menv.scalar("SELECT count(*) FROM invites WHERE used_at IS NOT NULL") == 0
    assert await menv.scalar("SELECT count(*) FROM pending_signups") == 1
    # Only hashes are stored: neither the token nor the password.
    stored = await menv.scalar("SELECT token_hash FROM pending_signups")
    assert stored != token.encode()
    password_hash = str(await menv.scalar("SELECT password_hash FROM pending_signups"))
    assert password_hash.startswith("$argon2id$")
    assert PASSWORD not in password_hash


async def test_an_existing_address_gets_the_same_answer_and_a_different_mail(
    menv: Env, smtp: SmtpFake
) -> None:
    code = await invite(menv)
    await menv.user(EMAIL)
    taken = await sign_up(Browser(menv), code)
    fresh = await sign_up(Browser(menv), code, email="other@example.com")
    assert (taken.status_code, taken.json()) == (fresh.status_code, fresh.json())
    assert await menv.scalar("SELECT count(*) FROM pending_signups") == 1  # only the free one
    [mail] = smtp.to(EMAIL)
    assert "already" in mail.text
    assert "#token=" not in mail.text
    assert await menv.scalar("SELECT count(*) FROM invites WHERE used_at IS NOT NULL") == 0


async def test_the_invite_is_checked_with_its_own_error(menv: Env, smtp: SmtpFake) -> None:
    for number, bad in enumerate((None, "", "not-an-invite")):
        response = await sign_up(Browser(menv), bad, email=f"bad{number}@example.com")
        assert (response.status_code, reason(response)) == (400, "invalid_invite")
    expired = await invite(menv)
    await menv.run("UPDATE invites SET expires_at = now() - interval '1 minute'")
    assert reason(await sign_up(Browser(menv), expired, "exp@example.com")) == "invalid_invite"
    assert smtp.messages == []


async def test_a_short_password_is_refused(menv: Env) -> None:
    response = await sign_up(Browser(menv), await invite(menv), password="short")
    assert (response.status_code, reason(response)) == (400, "password_rule")


async def test_closed_signups_refuse_everyone(app_database_url: str, clean_auth: None) -> None:
    async with running_env(app_database_url, signup_mode="closed") as env:
        response = await sign_up(Browser(env), None)
        assert (response.status_code, reason(response)) == (403, "signups_closed")


async def test_open_signups_are_refused_until_the_m8_gate(
    app_database_url: str, clean_auth: None
) -> None:
    """Classbeat's ``open`` mode (no invite) is not available before M8 (#10): the app does not
    start with it, and without an invite a sign-up is refused (``invite_required`` above)."""
    with pytest.raises(ValidationError, match="refused until the M8 gate"):
        async with running_env(app_database_url, signup_mode="open"):
            pass


async def confirm(browser: Browser, token: str, password: str = PASSWORD):
    return await browser.post("/api/auth/signup/confirm", {"token": token, "password": password})


async def pending(env: Env, smtp: SmtpFake, code: str) -> str:
    await sign_up(Browser(env), code)
    return smtp.to(EMAIL)[-1].token()


async def test_confirmation_creates_a_verified_user_and_consumes_the_invite(
    menv: Env, smtp: SmtpFake
) -> None:
    token = await pending(menv, smtp, await invite(menv))
    browser = Browser(menv)
    response = await confirm(browser, token)
    assert (response.status_code, response.json()) == (200, {"status": "created"})
    assert not browser.signed_in  # opening a mail link never signs the browser in
    assert await menv.scalar("SELECT email_verified_at IS NOT NULL FROM users") is True
    assert await menv.scalar("SELECT count(*) FROM pending_signups") == 0
    assert await menv.scalar("SELECT used_by IS NOT NULL FROM invites") is True
    assert (await browser.sign_in(EMAIL)).status_code == 200  # the chosen password works
    # The token is single use.
    again = await confirm(Browser(menv), token)
    assert (again.status_code, reason(again)) == (400, "invalid_token")


async def test_a_wrong_password_leaves_the_row_and_token_and_counts_as_a_failure(
    menv: Env, smtp: SmtpFake
) -> None:
    token = await pending(menv, smtp, await invite(menv))
    wrong = await confirm(Browser(menv), token, "another long password!")
    assert (wrong.status_code, reason(wrong)) == (400, "wrong_password")
    assert await menv.scalar("SELECT count(*) FROM pending_signups") == 1
    assert await menv.scalar("SELECT count(*) FROM users") == 0
    assert int(str(await menv.scalar("SELECT sum(count) FROM rate_limits WHERE count > 0"))) > 0
    assert (await confirm(Browser(menv), token)).status_code == 200  # the token still works


async def test_confirmation_failures_hit_the_sign_in_limits(menv: Env, smtp: SmtpFake) -> None:
    token = await pending(menv, smtp, await invite(menv))
    browser = Browser(menv)
    statuses = [
        (await confirm(browser, token, "wrong password 12345")).status_code for _ in range(12)
    ]
    assert statuses[:10] == [400] * 10
    assert statuses[10] == 429
    right = await confirm(browser, token)
    assert right.status_code == 429
    assert "retry-after" in right.headers


async def test_an_unknown_or_expired_token_is_refused(menv: Env, smtp: SmtpFake) -> None:
    unknown = await confirm(Browser(menv), "no-such-token")
    assert (unknown.status_code, reason(unknown)) == (400, "invalid_token")
    token = await pending(menv, smtp, await invite(menv))
    await menv.run("UPDATE pending_signups SET expires_at = now() - interval '1 second'")
    expired = await confirm(Browser(menv), token)
    assert (expired.status_code, reason(expired)) == (400, "invalid_token")
    assert await menv.scalar("SELECT count(*) FROM users") == 0


async def test_an_unconfirmed_signup_creates_nothing(menv: Env, smtp: SmtpFake) -> None:
    await pending(menv, smtp, await invite(menv))
    assert await menv.scalar("SELECT count(*) FROM users") == 0
    assert (await Browser(menv).sign_in(EMAIL)).status_code == 401


async def test_an_invite_used_between_request_and_confirmation_rolls_back(
    menv: Env, smtp: SmtpFake
) -> None:
    token = await pending(menv, smtp, await invite(menv))
    await menv.run("UPDATE invites SET used_at = now()")  # another sign-up won it
    response = await confirm(Browser(menv), token)
    assert (response.status_code, reason(response)) == (409, "invite_invalid")
    assert await menv.scalar("SELECT count(*) FROM users") == 0
    assert await menv.scalar("SELECT count(*) FROM pending_signups") == 1  # the row stays


async def test_an_expired_invite_fails_at_confirmation(menv: Env, smtp: SmtpFake) -> None:
    token = await pending(menv, smtp, await invite(menv))
    await menv.run("UPDATE invites SET expires_at = now() - interval '1 second'")
    assert reason(await confirm(Browser(menv), token)) == "invite_invalid"


async def test_two_pending_signups_for_one_invite_have_one_winner(
    menv: Env, smtp: SmtpFake
) -> None:
    code = await invite(menv)
    first = await pending(menv, smtp, code)
    await sign_up(Browser(menv), code, email="second@example.com")
    second = smtp.to("second@example.com")[0].token()
    assert (await confirm(Browser(menv), first)).status_code == 200
    lost = await confirm(Browser(menv), second)
    assert (lost.status_code, reason(lost)) == (409, "invite_invalid")


async def test_an_address_taken_meanwhile_creates_nothing_and_logs_no_address(
    menv: Env, smtp: SmtpFake, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    token = await pending(menv, smtp, await invite(menv))
    await menv.user(EMAIL.upper())  # the same normalized address
    response = await confirm(Browser(menv), token)
    assert (response.status_code, reason(response)) == (409, "account_exists")
    assert await menv.scalar("SELECT count(*) FROM users") == 1
    assert await menv.scalar("SELECT used_at FROM invites") is None  # rolled back
    assert EMAIL.lower() not in caplog.text.lower()


async def test_mail_limits_per_address_and_per_ip(menv: Env, smtp: SmtpFake) -> None:
    code = await invite(menv)
    statuses = [(await sign_up(Browser(menv), code)).status_code for _ in range(4)]
    assert statuses == [202, 202, 202, 429]  # 3 per address per hour
    limited = await sign_up(Browser(menv), code)
    assert "retry-after" in limited.headers
    more = [
        (await sign_up(Browser(menv), code, email=f"u{i}@example.com")).status_code
        for i in range(4)
    ]
    assert 429 in more  # 5 per IP per hour, counting the earlier requests


async def test_no_password_token_or_link_reaches_the_logs(
    menv: Env, smtp: SmtpFake, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    code = await invite(menv)
    token = await pending(menv, smtp, code)
    await confirm(Browser(menv), token, "wrong password 12345")
    await confirm(Browser(menv), token)
    for secret in (PASSWORD, "wrong password 12345", token, code, f"#token={token}"):
        assert secret not in caplog.text


async def test_a_failing_mail_server_does_not_change_the_answer_or_log_the_address(
    app_database_url: str, clean_auth: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with running_env(
        app_database_url, smtp_host="127.0.0.1", smtp_port=1, smtp_tls="none"
    ) as env:
        response = await sign_up(Browser(env), await invite(env))
    assert response.status_code == 202
    assert "mail not sent" in caplog.text
    assert EMAIL not in caplog.text


async def test_the_existing_address_branch_hashes_too_and_a_bad_invite_does_not(
    menv: Env,
) -> None:
    calls: list[str] = []
    hasher = menv.app.state.password_hasher
    original = hasher.hash

    async def spy(password: str) -> str:
        calls.append(password)
        return await original(password)

    hasher.hash = spy
    code = await invite(menv)
    await menv.user(EMAIL)
    assert (await sign_up(Browser(menv), code)).status_code == 202  # existing address
    assert len(calls) == 1
    refused = await sign_up(Browser(menv), "bad-invite", "other@example.com")
    assert reason(refused) == "invalid_invite"
    assert len(calls) == 1  # refused before hashing


async def test_a_pending_signup_is_not_confirmed_while_closed_but_waits_for_a_reopening(
    app_database_url: str, clean_auth: None, smtp: SmtpFake
) -> None:
    # ADR 0011 section 7, amendment 2026-10-31: "closed" also stops confirmation.
    mail = {"smtp_host": "127.0.0.1", "smtp_port": smtp.port, "smtp_tls": "none"}
    async with running_env(app_database_url, **mail) as env:
        await sign_up(Browser(env), await invite(env))
    token = smtp.to(EMAIL)[0].token()
    async with running_env(app_database_url, signup_mode="closed", **mail) as env:
        refused = await confirm(Browser(env), token)
        assert (refused.status_code, reason(refused)) == (403, "signups_closed")
        assert await env.scalar("SELECT count(*) FROM pending_signups") == 1
        assert await env.scalar("SELECT count(*) FROM users") == 0
        assert await env.scalar("SELECT count(*) FROM invites WHERE used_at IS NOT NULL") == 0
    async with running_env(app_database_url, **mail) as env:  # sign-ups reopen
        assert (await confirm(Browser(env), token)).status_code == 200
        assert await env.scalar("SELECT count(*) FROM invites WHERE used_at IS NOT NULL") == 1


async def test_validation_errors_on_auth_routes_do_not_echo_the_input(menv: Env) -> None:
    response = await Browser(menv).post(
        "/api/auth/signup", {"email": "not-an-address", "password": "secret-pass-phrase-1"}
    )
    assert response.status_code == 422
    assert "not-an-address" not in response.text
    assert "secret-pass-phrase-1" not in response.text
