"""The language of the mails web sends inside a request (ADR 0015 section 7; #253).

Sign-up confirmation, "account exists" and the reset link: the account's stored language if it has
one, otherwise the request's (the body's ``locale`` field, else ``Accept-Language``); mail before
an account exists always uses the request's. The CLI's reset link has no request: the stored
language, else ``en``. Each mail carries its code as ``Content-Language``.

Synthetic accounts only; mail goes to the in-process SMTP fake, Google is ``oidcfake``.
"""

import asyncio

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    running_env,
    smtp_fixture,
)
from flows import PASSWORD, Browser
from oauthtools import fake_fixture, location, make_invite, oenv_fixture, start  # noqa: F401 - fixtures
from oidcfake import FakeProviders
from ownvitals.auth.mail_texts import TABLES
from ownvitals.auth.reset_routes import make_reset_link_sender
from ownvitals.mail import Mailer
from smtpfake import SmtpFake
from test_auth_signup import EMAIL, invite

RESET = "/api/auth/password/reset/request"


def _language(smtp: SmtpFake, address: str) -> str:
    [mail] = smtp.to(address)
    return str(mail.message["Content-Language"])


@pytest.mark.parametrize(
    ("field", "header", "expected"),
    [("sv", None, "sv"), (None, "sv-SE,sv;q=0.9", "sv"), ("en", "sv", "en"), ("de", None, "en")],
)
async def test_a_confirmation_is_in_the_request_s_language(
    menv: Env, smtp: SmtpFake, field: str | None, header: str | None, expected: str
) -> None:
    if header is not None:
        menv.client.headers["accept-language"] = header
    body: dict[str, str | None] = {"email": EMAIL, "password": PASSWORD, "locale": field}
    body["invite_code"] = await invite(menv)
    assert (await Browser(menv).post("/api/auth/signup", body)).status_code == 202
    assert _language(smtp, EMAIL) == expected
    [mail] = smtp.to(EMAIL)
    assert mail.subject == TABLES[expected]["signup.subject"]


@pytest.mark.parametrize(
    ("stored", "field", "expected"), [("sv", "en", "sv"), ("en", "sv", "en"), (None, "sv", "sv")]
)
async def test_account_exists_is_in_the_account_s_language_else_the_request_s(
    menv: Env, smtp: SmtpFake, stored: str | None, field: str, expected: str
) -> None:
    await menv.user(EMAIL)
    await menv.run("UPDATE users SET locale = :l", l=stored)
    body = {"email": EMAIL, "password": PASSWORD, "invite_code": await invite(menv)}
    body["locale"] = field
    assert (await Browser(menv).post("/api/auth/signup", body)).status_code == 202
    assert _language(smtp, EMAIL) == expected
    assert smtp.to(EMAIL)[0].subject == TABLES[expected]["exists.subject"]


@pytest.mark.parametrize(
    ("stored", "header", "expected"), [("sv", "en", "sv"), (None, "sv", "sv"), (None, None, "en")]
)
async def test_a_reset_link_is_in_the_account_s_language_else_the_request_s(
    menv: Env, smtp: SmtpFake, stored: str | None, header: str | None, expected: str
) -> None:
    await menv.user(EMAIL)
    await menv.run("UPDATE users SET locale = :l", l=stored)
    if header is not None:
        menv.client.headers["accept-language"] = header
    assert (await Browser(menv).post(RESET, {"email": EMAIL})).status_code == 202
    assert _language(smtp, EMAIL) == expected
    assert smtp.to(EMAIL)[0].subject == TABLES[expected]["reset.subject"]


async def test_a_language_switched_off_falls_back_to_the_request_s(
    app_database_url: str, clean_auth: None, smtp: SmtpFake
) -> None:
    overrides = {"smtp_host": "127.0.0.1", "smtp_port": smtp.port, "smtp_tls": "none"}
    async with running_env(app_database_url, locales="en", **overrides) as env:
        await env.user(EMAIL)
        await env.run("UPDATE users SET locale = 'sv'")
        env.client.headers["accept-language"] = "sv"
        assert (await Browser(env).post(RESET, {"email": EMAIL})).status_code == 202
    assert _language(smtp, EMAIL) == "en"


@pytest.mark.parametrize(("stored", "expected"), [("sv", "sv"), (None, "en")])
async def test_the_cli_reset_link_is_in_the_stored_language_else_english(
    menv: Env, smtp: SmtpFake, stored: str | None, expected: str
) -> None:
    user = await menv.user(EMAIL)
    await menv.run("UPDATE users SET locale = :l", l=stored)
    send = make_reset_link_sender(menv.settings, menv.factory, Mailer(menv.settings))
    await send(user, EMAIL)
    assert _language(smtp, EMAIL) == expected


async def test_a_provider_confirmation_is_in_the_sign_up_s_language(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    """Before an account exists: the language in effect when the Google sign-up started, not the
    confirming request's header."""
    oenv.client.headers["accept-language"] = "sv"
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"], email_verified=False)
    assert location(await browser.get(callback)) == "/signin?oauth=email_needed"
    oenv.client.headers["accept-language"] = "en"
    await browser.post("/api/auth/oauth/pending/email", {"email": "new@example.com"})
    await asyncio.sleep(0.3)
    assert _language(smtp, "new@example.com") == "sv"
