"""OAuth sign-in: the flow, the callback-outcome table and the invite gate (ADR 0003 section 5)."""

import asyncio
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    running_env,
    smtp_fixture,
)
from flows import Browser, reason, user_with_password
from oauthtools import (  # noqa: F401 - fixtures
    CLIENT_IDS,
    fake_fixture,
    location,
    make_invite,
    oenv_fixture,
    sign_in_with,
    start,
)
from oidcfake import FakeProviders
from ownvitals.auth.cookies import OAUTH_COOKIE, SESSION_COOKIE
from smtpfake import SmtpFake


async def identities(env: Env) -> int:
    return int(str(await env.scalar("SELECT count(*) FROM identities")))


async def test_start_returns_the_authorization_url_and_sets_the_flow_cookie(oenv: Env) -> None:
    browser = Browser(oenv)
    response = await start(browser, "google")
    assert response.status_code == 200
    assert OAUTH_COOKIE in browser.cookies
    url = response.json()["authorization_url"]
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert query["response_mode"] == "query"
    assert query["code_challenge_method"] == "S256"
    assert {"state", "nonce", "code_challenge", "client_id"} <= set(query)
    assert "max_age" not in query
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert "max-age=600" in cookie
    stored = await oenv.scalar("SELECT flow_hash FROM oauth_flows")
    assert stored != browser.cookies[OAUTH_COOKIE].encode()  # only the hash


async def test_start_needs_the_csrf_token_and_a_provider_that_is_on(oenv: Env) -> None:
    browser = Browser(oenv)
    assert (await browser.post("/api/auth/oauth/google/start", {}, csrf="wrong")).status_code == 403
    assert (await start(browser, "nope")).status_code == 404


async def test_a_provider_without_its_secret_or_client_id_is_off(
    clean_auth: None, app_database_url: str
) -> None:
    async with running_env(
        app_database_url,
        oauth_google_client_id="google-client",  # no secret file
    ) as env:
        providers = (await Browser(env).get("/api/auth/oauth/providers")).json()["providers"]
        assert providers == []
        assert (await start(Browser(env), "google")).status_code == 404
        assert (await Browser(env).get("/oauth/google/callback?code=x&state=y")).status_code == 404
    async with running_env(
        app_database_url,
        oauth_google_client_secret="secret-only",  # no client id
    ) as env:
        providers = (await Browser(env).get("/api/auth/oauth/providers")).json()["providers"]
        assert providers == []


@pytest.mark.parametrize("provider", ["github", "microsoft"])
async def test_github_and_microsoft_are_not_providers(oenv: Env, provider: str) -> None:
    """#10 drops classbeat's GitHub and Microsoft sign-in: Google is the only provider."""
    providers = (await Browser(oenv).get("/api/auth/oauth/providers")).json()["providers"]
    assert providers == ["google"]
    assert (await start(Browser(oenv), provider)).status_code == 404
    assert (
        await Browser(oenv).get(f"/oauth/{provider}/callback?code=x&state=y")
    ).status_code == 404


async def test_new_google_user_with_a_verified_email_signs_up_and_in(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser = Browser(oenv)
    invite = await make_invite(oenv)
    response = await sign_in_with(browser, fake, "google", invite=invite)
    assert location(response) == "/"
    assert browser.signed_in
    assert await oenv.scalar("SELECT count(*) FROM users") == 1
    assert await oenv.scalar("SELECT email FROM users") == "g@example.com"
    assert await oenv.scalar("SELECT password_hash FROM users") is None
    assert await oenv.scalar("SELECT subject FROM identities") == "sub-1"
    assert await oenv.scalar("SELECT used_by IS NOT NULL FROM invites") is True
    assert await oenv.scalar("SELECT count(*) FROM oauth_flows") == 0
    await asyncio.sleep(0.3)
    [mail] = smtp.to("g@example.com")  # every new link mails the account address
    assert "Google" in mail.text
    assert (await browser.get("/api/auth/session")).json()["user"]["email"] == "g@example.com"


async def test_an_existing_identity_signs_in_without_an_invite(
    oenv: Env, fake: FakeProviders
) -> None:
    await sign_in_with(Browser(oenv), fake, "google", invite=await make_invite(oenv))
    second = Browser(oenv)
    response = await sign_in_with(second, fake, "google", return_to="/classes")
    assert location(response) == "/classes"
    assert second.signed_in
    assert await oenv.scalar("SELECT count(*) FROM users") == 1


async def test_return_to_must_be_a_relative_path(oenv: Env, fake: FakeProviders) -> None:
    await sign_in_with(Browser(oenv), fake, "google", invite=await make_invite(oenv))
    for bad in ("https://evil.example/", "//evil.example", "/\\evil", "javascript:1"):
        browser = Browser(oenv)
        assert location(await sign_in_with(browser, fake, "google", return_to=bad)) == "/"


async def test_sign_in_to_a_disabled_user_gets_the_generic_failure(
    oenv: Env, fake: FakeProviders
) -> None:
    await sign_in_with(Browser(oenv), fake, "google", invite=await make_invite(oenv))
    await oenv.run("UPDATE users SET disabled_at = now()")
    browser = Browser(oenv)
    assert location(await sign_in_with(browser, fake, "google")) == "/signin?oauth=invalid"
    assert not browser.signed_in


# ---- the callback-outcome table ---------------------------------------------------------


async def test_a_verified_email_that_matches_an_account_creates_and_links_nothing(
    oenv: Env, fake: FakeProviders
) -> None:
    await oenv.user("g@example.com")
    browser = Browser(oenv)
    response = await sign_in_with(browser, fake, "google", invite=await make_invite(oenv))
    assert location(response) == "/signin?oauth=account_exists"
    assert not browser.signed_in
    assert await identities(oenv) == 0
    assert await oenv.scalar("SELECT count(*) FROM users") == 1
    assert await oenv.scalar("SELECT used_at IS NULL FROM invites") is True


@pytest.mark.parametrize("mode", ["closed", "invite"])
async def test_without_a_valid_invite_nothing_is_created(
    clean_auth: None, app_database_url: str, fake: FakeProviders, mode: str
) -> None:
    async with running_env(
        app_database_url,
        signup_mode=mode,
        oauth_google_client_id=CLIENT_IDS["google"],
        oauth_google_client_secret=fake.secret,
    ) as env:
        env.app.state.oauth.transport = fake.transport
        browser = Browser(env)
        response = await sign_in_with(browser, fake, "google")
        assert location(response) == "/signin?oauth=invite_required"
        assert await env.scalar("SELECT count(*) FROM users") == 0
        assert await env.scalar("SELECT count(*) FROM pending_signups") == 0
        assert not browser.signed_in
        if mode == "closed":  # a closed mode stays closed even for a valid invite
            invite = await make_invite(env)
            response = await sign_in_with(Browser(env), fake, "google", invite=invite)
            assert location(response) == "/signin?oauth=invite_required"
            assert await env.scalar("SELECT used_at IS NULL FROM invites") is True


async def test_the_start_checks_a_given_invite_with_its_own_error(oenv: Env) -> None:
    response = await start(Browser(oenv), "google", invite_code="not-an-invite")
    assert (response.status_code, reason(response)) == (400, "invalid_invite")
    assert await oenv.scalar("SELECT count(*) FROM oauth_flows") == 0


async def test_an_invite_used_between_start_and_callback_is_reported_as_no_longer_valid(
    oenv: Env, fake: FakeProviders
) -> None:
    invite = await make_invite(oenv)
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=invite)
    await oenv.run("UPDATE invites SET used_at = now()")
    callback = fake.complete(started.json()["authorization_url"])
    assert location(await browser.get(callback)) == "/signin?oauth=invite_invalid"
    assert await oenv.scalar("SELECT count(*) FROM users") == 0


async def test_an_invite_is_consumed_atomically_by_parallel_callbacks(
    oenv: Env, fake: FakeProviders
) -> None:
    invite = await make_invite(oenv)
    browsers = [Browser(oenv) for _ in range(4)]
    callbacks: list[str] = []
    for number, browser in enumerate(browsers):
        started = await start(browser, "google", invite_code=invite)
        callbacks.append(
            fake.complete(
                started.json()["authorization_url"],
                sub=f"racer-{number}",
                email=f"racer{number}@example.com",
            )
        )
    results = await asyncio.gather(
        *(browser.get(path) for browser, path in zip(browsers, callbacks, strict=True))
    )
    outcomes = sorted(location(r) for r in results)
    assert outcomes.count("/") == 1
    assert outcomes.count("/signin?oauth=invite_invalid") == 3
    assert await oenv.scalar("SELECT count(*) FROM users") == 1
    assert await identities(oenv) == 1


# ---- state, nonce, replay ---------------------------------------------------------------


async def test_a_replayed_callback_finds_no_flow(oenv: Env, fake: FakeProviders) -> None:
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"])
    flow_cookie = dict(browser.cookies)
    assert location(await browser.get(callback)) == "/"
    replay = Browser(oenv)
    replay.cookies = flow_cookie  # the attacker holds the flow cookie and the URL
    assert location(await replay.get(callback)) == "/signin?oauth=invalid"
    assert not replay.signed_in
    assert fake.token_requests == 1  # the replay never reached the token endpoint


async def test_parallel_callbacks_take_the_flow_once(oenv: Env, fake: FakeProviders) -> None:
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"])
    twins: list[Browser] = []
    for _ in range(5):
        twin = Browser(oenv)
        twin.cookies = dict(browser.cookies)
        twins.append(twin)
    results = await asyncio.gather(*(twin.get(callback) for twin in twins))
    assert sorted(location(r) for r in results) == ["/"] + ["/signin?oauth=invalid"] * 4
    assert fake.token_requests == 1


async def test_state_mismatch_is_refused_and_burns_the_flow(oenv: Env, fake: FakeProviders) -> None:
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"], state="forged-state")
    assert location(await browser.get(callback)) == "/signin?oauth=invalid"
    assert fake.token_requests == 0
    assert await oenv.scalar("SELECT count(*) FROM oauth_flows") == 0
    assert not browser.signed_in


async def test_a_callback_without_the_flow_cookie_is_refused(
    oenv: Env, fake: FakeProviders
) -> None:
    started = await start(Browser(oenv), "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"])
    victim = Browser(oenv)  # login CSRF: this browser never started the flow
    assert location(await victim.get(callback)) == "/signin?oauth=invalid"
    assert fake.token_requests == 0
    assert await oenv.scalar("SELECT count(*) FROM users") == 0


async def test_the_flow_is_bound_to_its_provider(oenv: Env, fake: FakeProviders) -> None:
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"])
    wrong = callback.replace("/oauth/google/", "/oauth/github/")
    assert (await browser.get(wrong)).status_code == 404
    assert fake.token_requests == 0
    assert await oenv.scalar("SELECT count(*) FROM oauth_flows") == 1  # still usable at Google


async def test_an_expired_flow_is_refused(oenv: Env, fake: FakeProviders) -> None:
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    callback = fake.complete(started.json()["authorization_url"])
    await oenv.run("UPDATE oauth_flows SET expires_at = now() - interval '1 second'")
    assert location(await browser.get(callback)) == "/signin?oauth=invalid"


async def test_the_provider_denying_access_is_reported(oenv: Env) -> None:
    browser = Browser(oenv)
    started = await start(browser, "google")
    state = parse_qs(urlsplit(started.json()["authorization_url"]).query)["state"][0]
    response = await browser.get(f"/oauth/google/callback?error=access_denied&state={state}")
    assert location(response) == "/signin?oauth=denied"
    assert await oenv.scalar("SELECT count(*) FROM oauth_flows") == 0


@pytest.mark.parametrize(
    "bad",
    [
        {"nonce": "another-nonce"},
        {"aud": "another-client"},
        {"iss": "https://evil.example"},
        {"exp": 1},
        {"alg": "none"},
        {"alg": "HS256"},
        {"wrong_key": True},
        {"kid": "unknown-kid"},
    ],
)
async def test_a_bad_id_token_creates_nothing(
    oenv: Env, fake: FakeProviders, bad: dict[str, Any]
) -> None:
    bad = dict(bad)
    if bad.pop("wrong_key", False):
        bad["key"] = fake.other
    browser = Browser(oenv)
    response = await sign_in_with(browser, fake, "google", invite=await make_invite(oenv), **bad)
    assert location(response) == "/signin?oauth=invalid"
    assert not browser.signed_in
    assert await oenv.scalar("SELECT count(*) FROM users") == 0
    assert await oenv.scalar("SELECT count(*) FROM pending_signups") == 0
    assert await oenv.scalar("SELECT used_at IS NULL FROM invites") is True


async def test_a_wrong_client_secret_fails_at_the_token_endpoint(
    oenv: Env, fake: FakeProviders
) -> None:
    fake.secret = "rotated-by-the-provider"
    response = await sign_in_with(Browser(oenv), fake, "google", invite=await make_invite(oenv))
    assert location(response) == "/signin?oauth=invalid"


async def test_signing_in_again_replaces_the_browsers_session(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = Browser(oenv)
    await sign_in_with(browser, fake, "google", invite=await make_invite(oenv))
    old = browser.cookies[SESSION_COOKIE]
    await sign_in_with(browser, fake, "google")
    assert browser.cookies[SESSION_COOKIE] != old
    assert await oenv.scalar("SELECT count(*) FROM sessions") == 1


async def test_admin_emails_grant_at_oauth_sign_in(
    clean_auth: None, app_database_url: str, fake: FakeProviders
) -> None:
    async with running_env(
        app_database_url,
        admin_emails="G@example.com",
        oauth_google_client_id="google-client",
        oauth_google_client_secret=fake.secret,
    ) as env:
        env.app.state.oauth.transport = fake.transport
        await user_with_password(env, "g@example.com")
        response = await sign_in_with(Browser(env), fake, "google", invite=await make_invite(env))
        assert location(response) == "/signin?oauth=account_exists"
        await env.run("DELETE FROM users")
        await sign_in_with(Browser(env), fake, "google", invite=await make_invite(env))
        assert await env.scalar("SELECT role FROM users") == "admin"


# ---- logs ---------------------------------------------------------------------------------


async def test_no_code_state_token_or_secret_reaches_the_logs(
    oenv: Env, fake: FakeProviders, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    browser = Browser(oenv)
    started = await start(browser, "google", invite_code=await make_invite(oenv))
    await browser.get(fake.complete(started.json()["authorization_url"]))
    bad = Browser(oenv)  # and a failing one
    failed = await start(bad, "google")
    await bad.get(fake.complete(failed.json()["authorization_url"], alg="none"))
    planted = [fake.secret, browser.cookies[SESSION_COOKIE], *fake.codes]
    planted += [spec["id_token"] for spec in fake.codes.values() if "id_token" in spec]
    for response in (started, failed):
        planted.append(parse_qs(urlsplit(response.json()["authorization_url"]).query)["state"][0])
    text = "\n".join(f"{r.name} {r.getMessage()} {getattr(r, 'path', '')}" for r in caplog.records)
    for secret in planted:
        assert secret not in text
    assert "code=" not in text
    assert "/oauth/google/callback?" not in text
