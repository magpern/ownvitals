"""Password reset and password change (ADR 0003 sections 5 and 6)."""

import logging

import pytest

from authtools import Env
from ownvitals import auth
from flows import PASSWORD, Browser, age_reauth, only_user_id, reason, signed_in, user_with_password
from smtpfake import SmtpFake

EMAIL = "user@example.com"
NEW = "a brand new passphrase"


async def ask(browser: Browser, email: str = EMAIL):
    return await browser.post("/api/auth/password/reset/request", {"email": email})


async def complete(
    browser: Browser, token: str, password: str = NEW, remove: list[str] | None = None
):
    return await browser.post(
        "/api/auth/password/reset/complete",
        {"token": token, "password": password, "remove_methods": remove or []},
    )


async def link_identity(env: Env, user_id: object, provider: str = "github") -> str:
    return str(
        await env.scalar(
            "INSERT INTO identities (user_id, provider, subject) VALUES (:u, :p, :s) RETURNING id",
            u=user_id,
            p=provider,
            s=f"{provider}-subject",
        )
    )


async def test_the_request_answers_the_same_for_known_and_unknown_addresses(
    menv: Env, smtp: SmtpFake
) -> None:
    await user_with_password(menv)
    known = await ask(Browser(menv))
    unknown = await ask(Browser(menv), "nobody@example.com")
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    assert len(smtp.messages) == 1
    assert smtp.messages[0].to == EMAIL
    token = smtp.messages[0].token()
    assert f"/reset#token={token}" in smtp.messages[0].text
    stored = await menv.scalar(
        "SELECT token_hash FROM auth_tokens WHERE purpose = 'reset_password'"
    )
    assert stored != token.encode()
    lifetime = await menv.scalar("SELECT expires_at - created_at FROM auth_tokens")
    assert 29 * 60 <= lifetime.total_seconds() <= 31 * 60  # type: ignore[union-attr]


async def test_a_disabled_user_gets_the_same_answer_and_no_mail(menv: Env, smtp: SmtpFake) -> None:
    user_id = await user_with_password(menv)
    await menv.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=user_id)
    assert (await ask(Browser(menv))).status_code == 202
    assert smtp.messages == []


async def test_reset_mail_is_limited_per_address_and_per_ip(menv: Env, smtp: SmtpFake) -> None:
    await user_with_password(menv)
    codes = [(await ask(Browser(menv))).status_code for _ in range(4)]
    assert codes == [202, 202, 202, 429]
    unknown = [(await ask(Browser(menv), f"nobody{i}@example.com")).status_code for i in range(8)]
    assert 429 in unknown  # 10 per IP per hour; unknown addresses count the same


async def test_completing_a_reset_sets_the_password_and_signs_out_everywhere(
    menv: Env, smtp: SmtpFake
) -> None:
    browser = await signed_in(menv)
    user_id = await only_user_id(menv)
    other_user = await menv.user("other@example.com")
    await menv.login(user_id)
    await menv.login(other_user)
    assert await menv.scalar("SELECT count(*) FROM known_devices") == 1
    await ask(Browser(menv))
    await ask(Browser(menv))  # two outstanding tokens
    token = smtp.messages[0].token()
    response = await complete(Browser(menv), token)
    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert await menv.scalar("SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 0
    assert await menv.scalar("SELECT count(*) FROM sessions") == 1  # the other user's stays
    assert await menv.scalar("SELECT count(*) FROM known_devices") == 0
    assert await menv.scalar("SELECT count(*) FROM auth_tokens") == 0  # outstanding ones too
    assert not (await browser.get("/api/auth/session")).json()["authenticated"]
    fresh = Browser(menv)
    assert (await fresh.sign_in(EMAIL, PASSWORD)).status_code == 401
    assert (await fresh.sign_in(EMAIL, NEW)).status_code == 200


async def test_a_reset_token_works_once_and_not_after_it_expires(menv: Env, smtp: SmtpFake) -> None:
    await user_with_password(menv)
    await ask(Browser(menv))
    token = smtp.messages[0].token()
    assert (await complete(Browser(menv), token)).status_code == 200
    reused = await complete(Browser(menv), token)
    assert (reused.status_code, reason(reused)) == (400, "invalid_token")
    await ask(Browser(menv))
    await menv.run("UPDATE auth_tokens SET expires_at = now() - interval '1 second'")
    expired = await complete(Browser(menv), smtp.messages[1].token(), "yet another passphrase")
    assert (expired.status_code, reason(expired)) == (400, "invalid_token")
    assert (await complete(Browser(menv), "garbage")).status_code == 400


async def test_a_weak_password_does_not_burn_the_token(menv: Env, smtp: SmtpFake) -> None:
    await user_with_password(menv)
    await ask(Browser(menv))
    token = smtp.messages[0].token()
    weak = await complete(Browser(menv), token, "short")
    assert (weak.status_code, reason(weak)) == (400, "password_rule")
    assert (await complete(Browser(menv), token)).status_code == 200


async def test_the_reset_page_lists_and_removes_linked_methods_without_recent_auth(
    menv: Env, smtp: SmtpFake
) -> None:
    user_id = await user_with_password(menv)
    github = await link_identity(menv, user_id, "github")
    google = await link_identity(menv, user_id, "google")
    await ask(Browser(menv))
    token = smtp.messages[0].token()
    info = await Browser(menv).post("/api/auth/password/reset/info", {"token": token})
    assert info.status_code == 200
    assert {m["provider"] for m in info.json()["methods"]} == {"github", "google"}
    # Listing does not consume the token; no session or recent auth is involved.
    done = await complete(Browser(menv), token, remove=[github])
    assert done.status_code == 200
    remaining = await menv.scalar("SELECT id FROM identities")
    assert str(remaining) == google


async def test_removing_a_method_of_another_user_is_404_and_leaves_the_token(
    menv: Env, smtp: SmtpFake
) -> None:
    await user_with_password(menv)
    other = await menv.user("other@example.com")
    foreign = await link_identity(menv, other)
    await ask(Browser(menv))
    token = smtp.messages[0].token()
    for bogus in (foreign, "11111111-1111-1111-1111-111111111111"):
        refused = await complete(Browser(menv), token, remove=[bogus])
        assert refused.status_code == 404
    assert await menv.scalar("SELECT count(*) FROM identities") == 1
    assert (await complete(Browser(menv), token)).status_code == 200  # not consumed


async def test_info_with_a_bad_token_is_refused(menv: Env) -> None:
    response = await Browser(menv).post("/api/auth/password/reset/info", {"token": "nope"})
    assert (response.status_code, reason(response)) == (400, "invalid_token")


async def test_change_password_needs_recent_authentication(menv: Env) -> None:
    browser = await signed_in(menv)
    await age_reauth(menv)
    refused = await browser.post("/api/auth/password/change", {"new_password": NEW})
    assert (refused.status_code, reason(refused)) == (403, "reauth_required")
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    assert (
        await browser.post("/api/auth/password/change", {"new_password": NEW})
    ).status_code == 200
    assert (await Browser(menv).sign_in(EMAIL, NEW)).status_code == 200


async def test_change_password_revokes_other_sessions_and_outstanding_tokens(
    menv: Env, smtp: SmtpFake
) -> None:
    browser = await signed_in(menv)
    user_id = await only_user_id(menv)
    await menv.login(user_id)  # a second session
    await ask(Browser(menv))
    assert await menv.scalar("SELECT count(*) FROM auth_tokens") == 1
    assert (
        await browser.post("/api/auth/password/change", {"new_password": NEW})
    ).status_code == 200
    assert await menv.scalar("SELECT count(*) FROM sessions") == 1
    assert (await browser.get("/api/auth/session")).json()["authenticated"]
    assert await menv.scalar("SELECT count(*) FROM auth_tokens") == 0


async def test_change_password_rejects_a_weak_password(menv: Env) -> None:
    browser = await signed_in(menv)
    response = await browser.post("/api/auth/password/change", {"new_password": "short"})
    assert (response.status_code, reason(response)) == (400, "password_rule")


async def test_an_oauth_only_user_can_set_a_password_after_reauth(menv: Env) -> None:
    user_id = await menv.user(EMAIL)
    issued = await menv.login(user_id)
    browser = Browser(menv)
    browser.cookies[auth.SESSION_COOKIE] = issued.token
    assert (
        await browser.post("/api/auth/password/change", {"new_password": NEW})
    ).status_code == 200
    assert (await Browser(menv).sign_in(EMAIL, NEW)).status_code == 200


async def test_step_up_is_needed_in_addition_when_totp_is_confirmed(menv: Env) -> None:
    browser = await signed_in(menv)
    await menv.run(
        "INSERT INTO mfa_totp (user_id, secret_ciphertext, nonce, key_id, confirmed_at) "
        "SELECT id, 'x', 'y', 'app-v1', now() FROM users"
    )
    refused = await browser.post("/api/auth/password/change", {"new_password": NEW})
    assert (refused.status_code, reason(refused)) == (403, "stepup_required")
    await menv.run("UPDATE sessions SET stepup_at = now()")
    assert (
        await browser.post("/api/auth/password/change", {"new_password": NEW})
    ).status_code == 200


async def test_an_unconfirmed_totp_needs_no_step_up(menv: Env) -> None:
    browser = await signed_in(menv)
    await menv.run(
        "INSERT INTO mfa_totp (user_id, secret_ciphertext, nonce, key_id) "
        "SELECT id, 'x', 'y', 'app-v1' FROM users"
    )
    assert (
        await browser.post("/api/auth/password/change", {"new_password": NEW})
    ).status_code == 200


async def test_the_registered_sender_mails_the_reset_link_for_mfa_reset(
    menv: Env, smtp: SmtpFake
) -> None:
    """``admin mfa-reset`` calls ``send_reset_link``; the sender creates a token and mails it."""
    user_id = await menv.user(EMAIL)
    sender = auth.make_reset_link_sender(menv.settings, menv.factory, menv.app.state.mailer)
    auth.register_reset_link_sender(sender)
    try:
        assert await auth.send_reset_link(user_id, EMAIL) is True
    finally:
        auth.register_reset_link_sender(None)
    [mail] = smtp.to(EMAIL)
    assert (await complete(Browser(menv), mail.token())).status_code == 200


async def test_no_password_token_or_link_reaches_the_logs(
    menv: Env, smtp: SmtpFake, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    await user_with_password(menv)
    await ask(Browser(menv))
    token = smtp.messages[0].token()
    await complete(Browser(menv), token, NEW)
    await Browser(menv).sign_in(EMAIL, NEW)
    for secret in (PASSWORD, NEW, token, f"token={token}"):
        assert secret not in caplog.text


def spy_on_hashing(env: Env) -> list[str]:
    calls: list[str] = []
    hasher = env.app.state.password_hasher
    original = hasher.hash

    async def spy(password: str) -> str:
        calls.append(password)
        return await original(password)

    hasher.hash = spy
    return calls


async def trip_ip_limit(env: Env) -> None:
    from ownvitals.auth.clientip import bucket_ip
    from ownvitals.auth.ratelimit import SIGN_IN_IP, Bucket

    for _ in range(SIGN_IN_IP.max_count + 1):
        await env.app.state.limiter.begin([Bucket(SIGN_IN_IP, bucket_ip("127.0.0.1"))])


async def test_complete_never_hashes_for_a_tripped_limit_or_a_junk_token(
    menv: Env, smtp: SmtpFake
) -> None:
    await user_with_password(menv)
    await ask(Browser(menv))
    token = smtp.messages[0].token()
    calls = spy_on_hashing(menv)
    junk = await complete(Browser(menv), "junk-token")
    assert (junk.status_code, reason(junk)) == (400, "invalid_token")
    assert calls == []
    await trip_ip_limit(menv)
    limited = await complete(Browser(menv), token)
    assert limited.status_code == 429
    assert "retry-after" in limited.headers
    assert calls == []


async def test_a_successful_complete_releases_its_ip_slot(menv: Env, smtp: SmtpFake) -> None:
    await user_with_password(menv)
    await ask(Browser(menv))
    assert (await complete(Browser(menv), smtp.messages[0].token())).status_code == 200
    from ownvitals.auth.ratelimit import SIGN_IN_IP

    digest = menv.app.state.limiter.bucket_hash(SIGN_IN_IP, "127.0.0.1")
    assert await menv.scalar("SELECT count FROM rate_limits WHERE bucket_hash = :h", h=digest) == 0


async def test_the_security_notifier_mails_totp_and_mfa_failure_notices(
    menv: Env, smtp: SmtpFake
) -> None:
    import uuid

    from ownvitals.auth import notices

    # Registered at start-up by the web app.
    await notices.notify_totp_enrolled(uuid.uuid4(), "user@example.com")
    await notices.notify_mfa_failures(uuid.uuid4(), "user@example.com")
    subjects = [m.subject for m in smtp.to("user@example.com")]
    assert any("Two-factor" in s for s in subjects)
    assert any("Failed two-factor" in s for s in subjects)


async def test_reauth_for_a_totp_user_sets_recent_auth_but_not_step_up(menv: Env) -> None:
    browser = await signed_in(menv)
    await menv.run(
        "INSERT INTO mfa_totp (user_id, secret_ciphertext, nonce, key_id, confirmed_at) "
        "SELECT id, 'x', 'y', 'app-v1', now() FROM users"
    )
    await age_reauth(menv)
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    session = (await browser.get("/api/auth/session")).json()["session"]
    assert session["recent_auth"] is True
    assert session["stepup"] is False
    refused = await browser.post("/api/auth/password/change", {"new_password": NEW})
    assert (refused.status_code, reason(refused)) == (403, "stepup_required")


async def test_complete_checks_method_ownership_before_hashing(menv: Env, smtp: SmtpFake) -> None:
    await user_with_password(menv)
    other = await menv.user("other@example.com")
    foreign = await link_identity(menv, other)
    await ask(Browser(menv))
    calls = spy_on_hashing(menv)
    refused = await complete(Browser(menv), smtp.messages[0].token(), remove=[foreign])
    assert refused.status_code == 404
    assert calls == []


async def test_password_change_is_limited_per_user(menv: Env) -> None:
    browser = await signed_in(menv)
    codes = [
        (await browser.post("/api/auth/password/change", {"new_password": NEW})).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [200] * 10
    assert codes[10] == 429
