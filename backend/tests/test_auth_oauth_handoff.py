"""The hand-off for an unverified or missing provider email (ADR 0003 section 5)."""

import asyncio
from typing import Any

import pytest

from authtools import Env
from ownvitals.auth.cookies import PENDING_COOKIE
from flows import Browser, reason
from oauthtools import location, make_invite, sign_in_with
from oidcfake import DROP, TENANT, FakeProviders
from smtpfake import SmtpFake

SUBJECT = f"{TENANT}/oid-1"
NEEDS_EMAIL = "/signin?oauth=email_needed"


async def hand_off(env: Env, fake: FakeProviders, browser: Browser | None = None, **kwargs: Any):
    browser = browser or Browser(env)
    invite = kwargs.pop("invite", None) or await make_invite(env)
    provider = kwargs.pop("provider", "microsoft")
    response = await sign_in_with(browser, fake, provider, invite=invite, **kwargs)
    return browser, response


async def submit(browser: Browser, email: str, **extra: Any):
    return await browser.post("/api/auth/oauth/pending/email", {"email": email, **extra})


async def test_microsoft_creates_a_server_side_row_bound_to_the_browser(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, response = await hand_off(oenv, fake)
    assert location(response) == NEEDS_EMAIL
    assert await oenv.scalar("SELECT count(*) FROM users") == 0
    assert await oenv.scalar("SELECT provider FROM pending_signups") == "microsoft"
    assert await oenv.scalar("SELECT subject FROM pending_signups") == SUBJECT
    assert await oenv.scalar("SELECT invite_id IS NOT NULL FROM pending_signups") is True
    assert await oenv.scalar("SELECT email IS NULL AND token_hash IS NULL FROM pending_signups")
    assert await oenv.scalar(
        "SELECT expires_at <= now() + interval '15 minutes' FROM pending_signups"
    )
    cookie = browser.cookies[PENDING_COOKIE]
    assert await oenv.scalar("SELECT browser_hash FROM pending_signups") != cookie.encode()
    state = (await browser.get("/api/auth/oauth/pending")).json()
    assert state == {"pending": True, "provider": "microsoft", "awaiting_email": True}
    assert (await Browser(oenv).get("/api/auth/oauth/pending")).json()["pending"] is False


@pytest.mark.parametrize(
    ("provider", "options"),
    [
        ("google", {"email_verified": False}),
        ("google", {"email": DROP}),
        (
            "github",
            {"github_emails": [{"email": "u@example.com", "primary": True, "verified": False}]},
        ),
        ("github", {"github_emails": []}),
        (
            "github",
            {"github_emails": [{"email": "u@example.com", "primary": False, "verified": True}]},
        ),
    ],
)
async def test_an_unverified_or_missing_provider_email_hands_off_too(
    oenv: Env, fake: FakeProviders, provider: str, options: dict[str, Any]
) -> None:
    browser, response = await hand_off(oenv, fake, provider=provider, **options)
    assert location(response) == NEEDS_EMAIL
    assert await oenv.scalar("SELECT count(*) FROM users") == 0
    assert await oenv.scalar("SELECT count(*) FROM identities") == 0
    assert browser.cookies[PENDING_COOKIE]


async def test_the_hand_off_still_needs_an_invite(oenv: Env, fake: FakeProviders) -> None:
    browser = Browser(oenv)
    response = await sign_in_with(browser, fake, "microsoft")
    assert location(response) == "/signin?oauth=invite_required"
    assert await oenv.scalar("SELECT count(*) FROM pending_signups") == 0
    assert PENDING_COOKIE not in browser.cookies


async def test_an_existing_microsoft_identity_signs_in_instead(
    oenv: Env, fake: FakeProviders
) -> None:
    user = await oenv.user("me@example.com")
    await oenv.run(
        "INSERT INTO identities (user_id, provider, subject) VALUES (:u, 'microsoft', :s)",
        u=user,
        s=SUBJECT,
    )
    browser = Browser(oenv)
    assert location(await sign_in_with(browser, fake, "microsoft")) == "/"
    assert browser.signed_in
    assert await oenv.scalar("SELECT count(*) FROM pending_signups") == 0


async def test_a_second_pending_sign_up_in_the_same_browser_replaces_the_first(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, _ = await hand_off(oenv, fake, oid="first")
    await hand_off(oenv, fake, browser, oid="second")
    assert await oenv.scalar("SELECT count(*) FROM pending_signups") == 1
    assert await oenv.scalar("SELECT subject FROM pending_signups") == f"{TENANT}/second"


async def test_the_email_form_ignores_provider_subject_and_invite_from_the_client(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser, _ = await hand_off(oenv, fake)
    other_invite = await make_invite(oenv)
    forged = {
        "provider": "google",
        "subject": "victims-subject",
        "invite_code": other_invite,
        "invite_id": "00000000-0000-0000-0000-000000000000",
        "token_hash": "x",
    }
    response = await submit(browser, "new@example.com", **forged)
    assert (response.status_code, response.json()) == (202, {"status": "check_your_inbox"})
    assert await oenv.scalar("SELECT provider FROM pending_signups") == "microsoft"
    assert await oenv.scalar("SELECT subject FROM pending_signups") == SUBJECT
    await asyncio.sleep(0.3)
    [mail] = smtp.to("new@example.com")
    assert (
        await browser.post("/api/auth/signup/confirm", {"token": mail.token(), **forged})
    ).status_code == 200
    assert await oenv.scalar("SELECT provider FROM identities") == "microsoft"
    assert await oenv.scalar("SELECT subject FROM identities") == SUBJECT
    assert await oenv.scalar("SELECT email FROM users") == "new@example.com"
    # The invite of the row was consumed, the one the client sent was not.
    assert await oenv.scalar("SELECT count(*) FROM invites WHERE used_at IS NOT NULL") == 1
    assert await oenv.scalar("SELECT used_at IS NULL FROM invites WHERE used_by IS NULL") is True


async def test_without_a_pending_row_the_email_form_does_nothing(
    oenv: Env, fake: FakeProviders
) -> None:
    response = await submit(Browser(oenv), "new@example.com", provider="google", subject="x")
    assert (response.status_code, reason(response)) == (400, "no_pending_signup")
    assert await oenv.scalar("SELECT count(*) FROM pending_signups") == 0


async def test_confirmation_in_another_browser_creates_nothing_and_keeps_the_token(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser, _ = await hand_off(oenv, fake)
    await submit(browser, "new@example.com")
    await asyncio.sleep(0.3)
    token = smtp.to("new@example.com")[0].token()
    elsewhere = Browser(oenv)  # the mailbox owner's phone
    response = await elsewhere.post("/api/auth/signup/confirm", {"token": token})
    assert (response.status_code, reason(response)) == (400, "wrong_browser")
    assert await oenv.scalar("SELECT count(*) FROM users") == 0
    assert await oenv.scalar("SELECT count(*) FROM pending_signups") == 1  # token still unused
    assert await oenv.scalar("SELECT count(*) FROM invites WHERE used_at IS NOT NULL") == 0
    again = await browser.post("/api/auth/signup/confirm", {"token": token})  # the right browser
    assert again.status_code == 200
    assert await oenv.scalar("SELECT count(*) FROM users") == 1
    assert PENDING_COOKIE not in browser.cookies
    assert not browser.signed_in  # a mail link never signs in (section 4)
    assert await oenv.scalar("SELECT count(*) FROM pending_signups") == 0


async def test_an_address_with_an_account_keeps_the_row_without_email_or_token(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    await oenv.user("taken@example.com")
    browser, _ = await hand_off(oenv, fake)
    response = await submit(browser, "Taken@Example.com")
    assert (response.status_code, response.json()) == (202, {"status": "check_your_inbox"})
    assert await oenv.scalar("SELECT email IS NULL AND token_hash IS NULL FROM pending_signups")
    # Still the 15 minutes of the callback: no 24 h extension.
    assert await oenv.scalar(
        "SELECT expires_at <= now() + interval '15 minutes' FROM pending_signups"
    )
    await asyncio.sleep(0.3)
    [mail] = smtp.to("Taken@Example.com")
    assert "already" in mail.text
    assert "#token=" not in mail.text
    assert await oenv.scalar("SELECT count(*) FROM users") == 1


async def test_the_confirmation_link_is_valid_for_24_hours_and_expires(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser, _ = await hand_off(oenv, fake)
    await submit(browser, "new@example.com")
    assert await oenv.scalar("SELECT expires_at > now() + interval '23 hours' FROM pending_signups")
    await asyncio.sleep(0.3)
    token = smtp.to("new@example.com")[0].token()
    await oenv.run("UPDATE pending_signups SET expires_at = now() - interval '1 second'")
    response = await browser.post("/api/auth/signup/confirm", {"token": token})
    assert (response.status_code, reason(response)) == (400, "invalid_token")
    assert (await browser.get("/api/auth/oauth/pending")).json()["pending"] is False


async def test_an_invite_used_in_the_meantime_leaves_the_row_and_says_so(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser, _ = await hand_off(oenv, fake)
    await submit(browser, "new@example.com")
    await asyncio.sleep(0.3)
    token = smtp.to("new@example.com")[0].token()
    await oenv.run("UPDATE invites SET used_at = now()")
    response = await browser.post("/api/auth/signup/confirm", {"token": token})
    assert (response.status_code, reason(response)) == (409, "invite_invalid")
    assert await oenv.scalar("SELECT count(*) FROM pending_signups") == 1
    assert await oenv.scalar("SELECT count(*) FROM users") == 0


async def test_an_identity_linked_elsewhere_in_the_meantime_creates_nothing(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser, _ = await hand_off(oenv, fake)
    await submit(browser, "new@example.com")
    await asyncio.sleep(0.3)
    token = smtp.to("new@example.com")[0].token()
    other = await oenv.user("other@example.com")
    await oenv.run(
        "INSERT INTO identities (user_id, provider, subject) VALUES (:u, 'microsoft', :s)",
        u=other,
        s=SUBJECT,
    )
    response = await browser.post("/api/auth/signup/confirm", {"token": token})
    assert (response.status_code, reason(response)) == (409, "account_exists")
    assert await oenv.scalar("SELECT count(*) FROM users") == 1
    assert await oenv.scalar("SELECT count(*) FROM invites WHERE used_at IS NOT NULL") == 0


async def test_parallel_confirmations_create_one_account(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser, _ = await hand_off(oenv, fake)
    await submit(browser, "new@example.com")
    await asyncio.sleep(0.3)
    token = smtp.to("new@example.com")[0].token()
    csrf = await browser.csrf()
    results = await asyncio.gather(
        *(browser.post("/api/auth/signup/confirm", {"token": token}, csrf=csrf) for _ in range(4))
    )
    assert sorted(r.status_code for r in results) == [200, 400, 400, 400]
    assert await oenv.scalar("SELECT count(*) FROM users") == 1


async def test_a_provider_token_is_not_a_password_sign_up(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser, _ = await hand_off(oenv, fake)
    await submit(browser, "new@example.com")
    await asyncio.sleep(0.3)
    token = smtp.to("new@example.com")[0].token()
    nobody = Browser(oenv)
    response = await nobody.post("/api/auth/signup/confirm", {"token": token, "password": "x" * 14})
    assert (response.status_code, reason(response)) == (400, "wrong_browser")
