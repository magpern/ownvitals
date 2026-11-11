"""One ``WARNING`` line for every refused OAuth callback, and nothing secret in it (#247; ADR 0003
section 1, amendment of 2026-11-08, and section 9).

Each case drives one refusal through the real callback and checks the single line
``oauth callback refused: provider=… purpose=… outcome=… why=…``. Planted values (the code,
state, nonce, the ID token, the subject, the email, an ``error`` text) never appear in any log
record: claim *names* only.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture, smtp_fixture  # noqa: F401 - fixtures
from flows import Browser, age_reauth, pass_second_factor
from oauthtools import fake_fixture, location, make_invite, oenv_fixture, start  # noqa: F401 - fixtures
from oidcfake import FakeProviders

LOGGER = "ownvitals.auth.oauth_routes"
SUB = "PLANTED-SUBJECT-0123456789abcdef"
EMAIL = "planted-address-4567@example.com"
ERROR_TEXT = "PLANTED-ERROR-TEXT-89abcdef"

Case = Callable[[Env, FakeProviders, list[str]], Awaitable[str]]


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def _flow(browser: Browser, purpose: str, planted: list[str], **body: object) -> str:
    started = await start(browser, "google", purpose, **body)
    assert started.status_code == 200, started.text
    url = started.json()["authorization_url"]
    query = _query(url)
    planted += [query["state"], query["nonce"], query["code_challenge"]]
    return url


async def _complete(
    browser: Browser, fake: FakeProviders, url: str, planted: list[str], **claims: Any
) -> str:
    callback = fake.complete(url, sub=SUB, email=EMAIL, **claims)
    planted += list(fake.codes)
    planted += [spec["id_token"] for spec in fake.codes.values()]
    return location(await browser.get(callback))


async def _google_only(env: Env, fake: FakeProviders, planted: list[str]) -> Browser:
    browser = Browser(env)
    url = await _flow(browser, "sign_in", planted, invite_code=await make_invite(env))
    assert await _complete(browser, fake, url, planted) == "/overview"
    await pass_second_factor(env, browser, UUID(str(await env.scalar("SELECT id FROM users"))))
    await age_reauth(env)
    return browser


async def no_flow(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    planted.append("PLANTED-STATE-WITHOUT-FLOW")
    response = await Browser(env).get(
        "/oauth/google/callback?code=PLANTED-CODE-WITHOUT-FLOW&state=PLANTED-STATE-WITHOUT-FLOW"
    )
    planted.append("PLANTED-CODE-WITHOUT-FLOW")
    return location(response)


async def state_mismatch(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = Browser(env)
    url = await _flow(browser, "sign_in", planted)
    return await _complete(browser, fake, url, planted, state="PLANTED-WRONG-STATE")


async def provider_error_named(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = Browser(env)
    state = _query(await _flow(browser, "sign_in", planted))["state"]
    return location(await browser.get(f"/oauth/google/callback?error=access_denied&state={state}"))


async def provider_error_unknown(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = Browser(env)
    state = _query(await _flow(browser, "sign_in", planted))["state"]
    planted.append(ERROR_TEXT)
    return location(await browser.get(f"/oauth/google/callback?error={ERROR_TEXT}&state={state}"))


async def no_code(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = Browser(env)
    state = _query(await _flow(browser, "sign_in", planted))["state"]
    return location(await browser.get(f"/oauth/google/callback?state={state}"))


async def bad_nonce(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = Browser(env)
    url = await _flow(browser, "sign_in", planted)
    return await _complete(browser, fake, url, planted, nonce="PLANTED-OTHER-NONCE")


async def invite_required(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = Browser(env)
    url = await _flow(browser, "sign_in", planted)
    return await _complete(browser, fake, url, planted)


async def reauth_future(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = await _google_only(env, fake, planted)
    url = await _flow(browser, "reauth", planted)
    return await _complete(browser, fake, url, planted, auth_time=int(time.time()) + 3600)


async def reauth_password(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = await _google_only(env, fake, planted)
    url = await _flow(browser, "reauth", planted)
    await env.run("UPDATE users SET password_hash = 'set-meanwhile'")
    return await _complete(browser, fake, url, planted)


async def reauth_other_identity(env: Env, fake: FakeProviders, planted: list[str]) -> str:
    browser = await _google_only(env, fake, planted)
    url = await _flow(browser, "reauth", planted)
    callback = fake.complete(url, sub="PLANTED-OTHER-SUBJECT", email=EMAIL)
    planted += ["PLANTED-OTHER-SUBJECT", *fake.codes]
    return location(await browser.get(callback))


CASES: dict[str, tuple[Case, str, str]] = {
    # name: (case, "purpose outcome why", the page)
    "no_flow": (no_flow, "unknown invalid no_flow", "/signin?oauth=invalid"),
    "state_mismatch": (state_mismatch, "sign_in invalid state_mismatch", "/signin?oauth=invalid"),
    "provider_error": (
        provider_error_named,
        "sign_in denied provider_error=access_denied",
        "/signin?oauth=denied",
    ),
    "unknown_error": (
        provider_error_unknown,
        "sign_in denied provider_error=unrecognized",
        "/signin?oauth=denied",
    ),
    "no_code": (no_code, "sign_in denied no_code", "/signin?oauth=denied"),
    "bad_nonce": (bad_nonce, "sign_in invalid provider: nonce mismatch", "/signin?oauth=invalid"),
    "invite_required": (
        invite_required,
        "sign_in invite_required no_invite",
        "/signin?oauth=invite_required",
    ),
    "auth_time_future": (
        reauth_future,
        "reauth reauth_stale auth_time_future",
        "/account?oauth=reauth_stale",
    ),
    "has_password": (
        reauth_password,
        "reauth reauth_use_password has_password",
        "/account?oauth=reauth_use_password",
    ),
    "identity_mismatch": (
        reauth_other_identity,
        "reauth identity_mismatch not_linked_to_this_user",
        "/account?oauth=identity_mismatch",
    ),
}


def _refusals(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == LOGGER and r.getMessage().startswith("oauth callback refused")
    ]


@pytest.mark.parametrize("name", list(CASES))
async def test_every_refused_callback_logs_one_warning_without_a_secret(
    oenv: Env, fake: FakeProviders, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    case, expected, page = CASES[name]
    planted: list[str] = [fake.secret, SUB, EMAIL]
    caplog.set_level(logging.DEBUG)
    assert await case(oenv, fake, planted) == page
    purpose, outcome, why = expected.split(" ", 2)
    assert _refusals(caplog) == [
        f"oauth callback refused: provider=google purpose={purpose} outcome={outcome} why={why}"
    ]
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.WARNING]
    text = "\n".join(
        f"{r.name} {r.getMessage()} {getattr(r, 'path', '')} {r.args!r}" for r in caplog.records
    )
    for secret in planted:
        assert secret not in text, name
    assert "code=" not in text


async def test_an_accepted_callback_logs_no_refusal(
    oenv: Env, fake: FakeProviders, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    planted: list[str] = []
    browser = await _google_only(oenv, fake, planted)
    url = await _flow(browser, "reauth", planted)
    assert await _complete(browser, fake, url, planted) == "/account?oauth=reauthed"
    assert _refusals(caplog) == []
