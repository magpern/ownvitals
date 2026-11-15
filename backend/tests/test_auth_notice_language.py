"""The language of the security notices (ADR 0015 section 7, amended 2026-11-10; #282).

Every security notice uses the recipient's stored ``users.locale`` when it is sent, otherwise the
request's language (``Accept-Language`` here), otherwise ``en``: email changed, identity linked
(link, Google sign-up and the hand-off confirmation), passkey added and suspended, QR login
approved, TOTP enrolled and failed second factors (at sign-in, at a TOTP enrolment and at a QR
approval). The lookup
is ``auth/recipients.py`` (guard 8); the texts stay in the fixed per-locale tables.

Synthetic accounts only; mail goes to the in-process SMTP fake, Google is ``oidcfake``.
"""

import asyncio

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    menv_fixture,
    smtp_fixture,
)
from flows import Browser, only_user_id, signed_in
from oauthtools import fake_fixture, location, make_invite, oenv_fixture, sign_in_with, start  # noqa: F401 - fixtures
from oidcfake import FakeProviders
from ownvitals.auth import totp
from ownvitals.auth.mail_texts import TABLES
from passkeytools import SoftAuthenticator, failed, register, sign_in
from qrtools import QrBrowser, approve, approved, complete, phone, scan
from qrtools import start as qr_start
from smtpfake import Received, SmtpFake
from test_auth_email import NEW, change, confirm
from test_auth_passkeys_signcount import with_passkey

EMAIL = "user@example.com"
# (stored users.locale, Accept-Language, expected): the stored code wins over the request's,
# NULL falls back to the request's, and with neither the notice is English.
CASES = [("sv", "en", "sv"), ("en", "sv", "en"), (None, "sv-SE,sv;q=0.9", "sv"), (None, None, "en")]


async def _notice(smtp: SmtpFake, address: str, key: str) -> Received:
    """The one notice of ``key`` (``passkey_added`` …) to ``address``, in whichever language."""
    subjects = {table[f"{key}.subject"] for table in TABLES.values()}
    for _ in range(100):
        found = [m for m in smtp.to(address) if m.subject in subjects]
        if found:
            break
        await asyncio.sleep(0.05)
    [mail] = [m for m in smtp.to(address) if m.subject in subjects]
    return mail


async def _expect(smtp: SmtpFake, address: str, key: str, expected: str) -> None:
    mail = await _notice(smtp, address, key)
    assert str(mail.message["Content-Language"]) == expected
    assert mail.subject == TABLES[expected][f"{key}.subject"]


async def _set(env: Env, stored: str | None, header: str | None) -> None:
    await env.run("UPDATE users SET locale = :l", l=stored)
    if header is None:
        env.client.headers.pop("accept-language", None)
    else:
        env.client.headers["accept-language"] = header


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_email_changed_notice(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    browser = await signed_in(menv)
    await change(browser)
    token = smtp.to(NEW)[0].token()
    await _set(menv, stored, header)
    assert (await confirm(browser, token)).status_code == 200
    await _expect(smtp, EMAIL, "email_changed", expected)  # to the old address


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_passkey_added_notice(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    browser = await signed_in(menv)
    await _set(menv, stored, header)
    assert (await register(browser, SoftAuthenticator())).status_code == 201
    await _expect(smtp, EMAIL, "passkey_added", expected)


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_passkey_suspended_notice(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    auth = await with_passkey(menv)
    assert (await sign_in(Browser(menv), auth, sign_count=5)).status_code == 200
    await _set(menv, stored, header)
    assert failed(await sign_in(Browser(menv), auth, sign_count=3))
    await _expect(smtp, EMAIL, "passkey_suspended", expected)


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_qr_login_approved_notice(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    desktop = QrBrowser(menv)
    qr_id = await approved(menv, desktop, await phone(menv))
    await _set(menv, stored, header)
    assert (await complete(desktop, qr_id)).status_code == 200
    await _expect(smtp, EMAIL, "qr_approved", expected)


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_totp_enrolled_notice(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    browser = await signed_in(menv, second_factor=False)
    await _set(menv, stored, header)
    started = (await browser.post("/api/auth/mfa/totp/start")).json()
    code = totp.code_for_step(started["secret"], int(totp.clock() // totp.PERIOD))
    body = {"pending": started["pending"], "code": code}
    assert (await browser.post("/api/auth/mfa/totp/confirm", body)).status_code == 200
    await _expect(smtp, EMAIL, "totp_enrolled", expected)


async def _four_failures(env: Env) -> None:
    """Four ``mfa_failed`` in the last hour: the next failure is the fifth and mails."""
    await env.run(
        "INSERT INTO auth_events (user_id, kind) SELECT id, 'mfa_failed' FROM users, "
        "generate_series(1, 4)"
    )


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_mfa_failures_notice_at_a_totp_enrolment(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    browser = await signed_in(menv, second_factor=False)
    started = (await browser.post("/api/auth/mfa/totp/start")).json()
    await _four_failures(menv)
    await _set(menv, stored, header)
    body = {"pending": started["pending"], "code": "000000"}
    assert (await browser.post("/api/auth/mfa/totp/confirm", body)).status_code == 400
    await _expect(smtp, EMAIL, "mfa_failures", expected)


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_mfa_failures_notice_at_sign_in(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    browser = await signed_in(menv, second_factor=False)
    await menv.enrol(await only_user_id(menv))
    await _four_failures(menv)
    await _set(menv, stored, header)
    assert (await browser.post("/api/auth/mfa/verify", {"code": "000000"})).status_code == 400
    await _expect(smtp, EMAIL, "mfa_failures", expected)


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_mfa_failures_notice_at_a_qr_approval(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    mobile = await phone(menv)
    started = await qr_start(QrBrowser(menv))
    assert (await scan(mobile, started)).status_code == 200
    await _four_failures(menv)
    await _set(menv, stored, header)
    assert (await approve(mobile, started["id"], "000000")).status_code == 400
    await _expect(smtp, EMAIL, "mfa_failures", expected)


@pytest.mark.parametrize(("stored", "header", "expected"), CASES)
async def test_the_identity_linked_notice_of_a_link(
    oenv: Env,
    fake: FakeProviders,
    smtp: SmtpFake,
    stored: str | None,
    header: str | None,
    expected: str,
) -> None:
    browser = await signed_in(oenv, EMAIL)
    await _set(oenv, stored, header)
    linked = await sign_in_with(browser, fake, "google", purpose="link", sub="gsub")
    assert location(linked) == "/account?oauth=linked"
    await _expect(smtp, EMAIL, "identity_linked", expected)


@pytest.mark.parametrize("language", ["sv", "en"])
async def test_the_identity_linked_notice_of_a_google_sign_up(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake, language: str
) -> None:
    """The account is new: its stored language is the one of the sign-up's start."""
    oenv.client.headers["accept-language"] = language
    browser = Browser(oenv)
    invite = await make_invite(oenv)
    outcome = await sign_in_with(browser, fake, "google", invite=invite, email="new@example.com")
    assert outcome.status_code == 303
    assert await oenv.scalar("SELECT locale FROM users") == language
    await _expect(smtp, "new@example.com", "identity_linked", language)


async def test_the_identity_linked_notice_of_a_hand_off_uses_the_sign_up_s_language(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    """Confirmed from a request in another language: the stored (sign-up) language wins."""
    oenv.client.headers["accept-language"] = "sv"
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"], email_verified=False)
    assert location(await browser.get(callback)) == "/signin?oauth=email_needed"
    oenv.client.headers["accept-language"] = "en"
    await browser.post("/api/auth/oauth/pending/email", {"email": "new@example.com"})
    [confirmation] = await _wait_for(smtp, "new@example.com")
    answer = await browser.post("/api/auth/signup/confirm", {"token": confirmation.token()})
    assert answer.status_code == 200, answer.text
    await _expect(smtp, "new@example.com", "identity_linked", "sv")


async def _wait_for(smtp: SmtpFake, address: str) -> list[Received]:
    for _ in range(100):
        if smtp.to(address):
            break
        await asyncio.sleep(0.05)
    return smtp.to(address)
