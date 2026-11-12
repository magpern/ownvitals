"""Turnstile on sign-up, sign-in and the reset request (ADR 0018), through the routes.

The verifier is a fake (``FakeVerifier``): no request leaves the test, and the real egress call is
made to fail the test if anything reaches it. Every key, token and address is synthetic.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from authtools import (  # noqa: F401 - fixtures
    ORIGIN,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    running_env,
)
from flows import PASSWORD, Browser, user_with_password
from ownvitals.auth import turnstile as turnstile_module

ON: dict[str, Any] = {
    "turnstile_enabled": True,
    "turnstile_site_key": "synthetic-site-key",
    "turnstile_secret_key": "synthetic-turnstile-secret",
}
TOKEN = "synthetic.token"
EMAIL = "someone@example.com"
FORMS = [
    ("/api/auth/signup", {"email": EMAIL, "password": PASSWORD, "invite_code": "x"}, "signup"),
    ("/api/auth/login", {"email": EMAIL, "password": PASSWORD}, "signin"),
    ("/api/auth/password/reset/request", {"email": EMAIL}, "reset"),
]


class FakeVerifier:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def verify(self, token: str, action: str) -> bool:
        self.calls.append((token, action))
        return self.answer


@pytest.fixture(autouse=True)
def _no_real_call(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(**_: object) -> None:
        raise AssertionError("the real Turnstile call was reached")

    monkeypatch.setattr(turnstile_module, "verify_turnstile_token", refuse)


@pytest.fixture(name="off")
async def off_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    async with running_env(app_database_url) as running:
        yield running


@pytest.fixture(name="on")
async def on_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    async with running_env(app_database_url, **ON) as running:
        yield running


async def post(env: Env, path: str, body: object, tokens: list[str]) -> httpx.Response:
    browser = Browser(env)
    csrf = await browser.csrf()
    headers = [("Origin", ORIGIN), ("X-CSRF-Token", csrf), *browser._cookie_header().items()]  # pyright: ignore[reportPrivateUsage]
    headers += [("X-Turnstile-Token", token) for token in tokens]
    env.client.cookies.clear()
    return await env.client.post(path, json=body, headers=headers)


async def failed_sign_ins(env: Env) -> object:
    return await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'sign_in_failed'")


async def rate_rows(env: Env) -> object:
    return await env.scalar("SELECT count(*) FROM rate_limits")


# --- off ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "body", "action"), FORMS)
async def test_off_the_forms_need_no_token_and_nothing_is_called(
    off: Env, path: str, body: object, action: str
) -> None:
    assert off.app.state.turnstile is None
    response = await post(off, path, body, [])
    own = {"signup": (400, "invalid_invite"), "signin": (401, "invalid_credentials")}
    if action in own:  # the form's own answer, not Turnstile's
        assert (response.status_code, response.json()["detail"]["reason"]) == own[action]
    else:
        assert response.status_code == 202


async def test_off_the_session_says_so(off: Env) -> None:
    assert (await Browser(off).get("/api/auth/session")).json()["turnstile"] is None


# --- on ----------------------------------------------------------------------------------------


async def test_on_the_session_hands_the_page_its_site_key_and_script(on: Env) -> None:
    config = (await Browser(on).get("/api/auth/session")).json()["turnstile"]
    assert config == {
        "site_key": "synthetic-site-key",
        "script_url": "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
    }
    response = await Browser(on).get("/api/auth/session")
    assert "challenges.cloudflare.com" in response.headers["content-security-policy"]


@pytest.mark.parametrize(("path", "body", "action"), FORMS)
@pytest.mark.parametrize("tokens", [[], [""], [TOKEN, TOKEN]])
async def test_on_a_missing_token_is_refused_before_anything_else(
    on: Env, path: str, body: object, action: str, tokens: list[str]
) -> None:
    fake = FakeVerifier(True)
    on.app.state.turnstile = fake
    response = await post(on, path, body, tokens)
    assert (response.status_code, response.json()) == (
        400,
        {"detail": {"reason": "turnstile_failed"}},
    )
    assert fake.calls == []
    assert await rate_rows(on) == 0  # no rate limit counted, no database work
    assert await failed_sign_ins(on) == 0


@pytest.mark.parametrize(("path", "body", "action"), FORMS)
async def test_on_a_token_the_verifier_refuses_is_refused(
    on: Env, path: str, body: object, action: str
) -> None:
    fake = FakeVerifier(False)  # invalid, expired, a timeout: the verifier says no
    on.app.state.turnstile = fake
    response = await post(on, path, body, [TOKEN])
    assert (response.status_code, response.json()["detail"]["reason"]) == (400, "turnstile_failed")
    assert fake.calls == [(TOKEN, action)]
    assert await rate_rows(on) == 0
    assert await failed_sign_ins(on) == 0


@pytest.mark.parametrize(("path", "body", "action"), FORMS)
async def test_on_a_passing_token_lets_the_form_do_its_own_work(
    on: Env, path: str, body: object, action: str
) -> None:
    fake = FakeVerifier(True)
    on.app.state.turnstile = fake
    response = await post(on, path, body, [TOKEN])
    assert fake.calls == [(TOKEN, action)]
    expected = {"signup": (400, "invalid_invite"), "signin": (401, "invalid_credentials")}
    if action in expected:
        assert (response.status_code, response.json()["detail"]["reason"]) == expected[action]
    else:
        assert response.status_code == 202


async def test_on_a_password_sign_in_works_with_a_passing_token(on: Env) -> None:
    await user_with_password(on, EMAIL)
    on.app.state.turnstile = FakeVerifier(True)
    response = await post(on, "/api/auth/login", {"email": EMAIL, "password": PASSWORD}, [TOKEN])
    assert response.status_code == 200


async def test_on_without_a_verifier_every_form_fails_closed(on: Env) -> None:
    on.app.state.turnstile = None
    for path, body, _ in FORMS:
        response = await post(on, path, body, [TOKEN])
        assert (response.status_code, response.json()["detail"]["reason"]) == (
            400,
            "turnstile_failed",
        )


async def test_other_routes_are_not_gated(on: Env) -> None:
    fake = FakeVerifier(False)
    on.app.state.turnstile = fake
    response = await post(on, "/api/auth/password/reset/info", {"token": "synthetic"}, [])
    assert response.json()["detail"]["reason"] != "turnstile_failed"
    assert fake.calls == []
