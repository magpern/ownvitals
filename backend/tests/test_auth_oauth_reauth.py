"""Google re-authentication (ADR 0003 section 1, amendment of 2026-11-08, #247).

The fake behaves like the live Google: its ID tokens carry no ``auth_time``, whatever the request
asked for (``FakeProviders.honours_claims_request``). An account without a password is
re-authenticated by Google's answer to this flow; an account with a password never is (it
confirms with the password). A recent authentication is never a step-up: every action that needs
one also needs a fresh TOTP, unchanged.
"""

import json
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from uuid import UUID

import pytest
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    TEST_TOTP_SECRET,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    smtp_fixture,
)
from flows import PASSWORD, Browser, age_reauth, pass_second_factor, reason, signed_in
from oauthtools import fake_fixture, location, make_invite, oenv_fixture, sign_in_with, start  # noqa: F401 - fixtures
from oidcfake import DROP, FakeProviders
from ownvitals.auth import totp
from ownvitals.auth.cookies import SESSION_COOKIE

REAUTHED = "/account?oauth=reauthed"
RECENT = "SELECT reauth_at > now() - interval '1 minute' FROM sessions"
STALE = "SELECT reauth_at < now() - interval '5 minutes' FROM sessions"


async def google_only(env: Env, fake: FakeProviders) -> tuple[Browser, UUID]:
    """A full session of an account that signed up with Google (no password) and enrolled TOTP."""
    browser = Browser(env)
    response = await sign_in_with(browser, fake, "google", invite=await make_invite(env))
    assert location(response) == "/"
    user_id = UUID(str(await env.scalar("SELECT id FROM users")))
    await pass_second_factor(env, browser, user_id)
    return browser, user_id


async def reauth(browser: Browser, fake: FakeProviders, **claims: Any):
    return await sign_in_with(browser, fake, "google", purpose="reauth", **claims)


async def column(env: Env, sql: str) -> list[Any]:
    async with env.db() as db:
        return list((await db.execute(text(sql))).scalars().all())


async def failures(env: Env) -> list[Any]:
    """The ``detail`` of every ``reauth_failed`` event, oldest first."""
    return await column(
        env, "SELECT detail FROM auth_events WHERE kind = 'reauth_failed' ORDER BY at"
    )


async def refused_unchanged(env: Env, browser: Browser, old: str) -> None:
    assert browser.cookies[SESSION_COOKIE] == old
    assert await env.scalar(STALE) is True


# ---- the reproduction (#247): Google sends no auth_time --------------------------------------


async def test_a_google_only_account_reauthenticates_although_google_sends_no_auth_time(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    assert location(await reauth(browser, fake)) == REAUTHED
    assert browser.cookies[SESSION_COOKIE] != old  # rotated
    assert await oenv.scalar("SELECT count(*) FROM sessions") == 1
    assert await oenv.scalar(RECENT) is True
    session = (await browser.get("/api/auth/session")).json()["session"]
    assert session["recent_auth"] is True
    kinds = await column(oenv, "SELECT kind FROM auth_events WHERE kind LIKE 'reauth%'")
    assert kinds == ["reauth"]


async def test_the_reauth_request_keeps_max_age_and_the_claims_request_and_no_prompt(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, _ = await google_only(oenv, fake)
    started = await start(browser, "google", "reauth")
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    assert query["max_age"] == ["0"]
    assert "prompt" not in query  # Google does not document prompt=login
    assert json.loads(query["claims"][0]) == {"id_token": {"auth_time": {"essential": True}}}


async def test_google_sign_in_and_link_do_not_ask_for_auth_time(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await signed_in(oenv, "me@example.com")
    started = await start(browser, "google", "link")
    assert "claims" not in parse_qs(urlsplit(started.json()["authorization_url"]).query)


@pytest.mark.parametrize(
    ("auth_time", "expected"),
    [
        (DROP, REAUTHED),
        (int(time.time()) - 3600, REAUTHED),  # an old Google sign-in: accepted, bound to the flow
        (int(time.time()), REAUTHED),
        (int(time.time()) + 30, REAUTHED),  # within the 60 s skew
        (int(time.time()) + 3600, "/account?oauth=reauth_stale"),
    ],
    ids=["missing", "old", "fresh", "slightly ahead", "future"],
)
async def test_auth_time_of_a_google_only_account(
    oenv: Env, fake: FakeProviders, auth_time: object, expected: str
) -> None:
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    assert location(await reauth(browser, fake, auth_time=auth_time)) == expected
    if expected != REAUTHED:
        await refused_unchanged(oenv, browser, old)
        assert await failures(oenv) == [{"why": "auth_time_future", "provider": "google"}]


async def test_a_provider_that_answers_the_claims_request_is_accepted_too(
    oenv: Env, fake: FakeProviders
) -> None:
    fake.honours_claims_request = True  # a fresh auth_time, as after a real sign-in at Google
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    assert location(await reauth(browser, fake)) == REAUTHED


def _with_prompt(url: str, prompt: str | None) -> str:
    if prompt is None:
        return url
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()} | {"prompt": prompt}
    return urlunsplit(parts._replace(query=urlencode(query)))


@pytest.mark.parametrize("prompt", [None, "login"])
@pytest.mark.parametrize("auth_time", [DROP, int(time.time()) + 3600], ids=["missing", "future"])
async def test_prompt_login_plays_no_part_in_the_rule(
    oenv: Env, fake: FakeProviders, prompt: str | None, auth_time: object
) -> None:
    """Nothing in Google's answer shows that ``prompt=login`` was honoured, so the outcome is the
    same with and without it."""
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    started = await start(browser, "google", "reauth")
    url = _with_prompt(started.json()["authorization_url"], prompt)
    callback = fake.complete(url, auth_time=auth_time)
    expected = REAUTHED if auth_time is DROP else "/account?oauth=reauth_stale"
    assert location(await browser.get(callback)) == expected


# ---- accounts with a password ------------------------------------------------------------------


async def test_an_account_with_a_password_is_refused_at_the_start_and_no_flow_is_made(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await signed_in(oenv, "me@example.com")
    assert location(await sign_in_with(browser, fake, "google", purpose="link")) == (
        "/account?oauth=linked"
    )
    await age_reauth(oenv)
    response = await start(browser, "google", "reauth")
    assert (response.status_code, reason(response)) == (403, "reauth_use_password")
    assert await oenv.scalar("SELECT count(*) FROM oauth_flows") == 0
    # It confirms with its password instead, unchanged.
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200


@pytest.mark.parametrize("auth_time", [DROP, int(time.time())], ids=["missing", "fresh"])
async def test_a_password_set_after_the_start_is_refused_at_the_callback(
    oenv: Env, fake: FakeProviders, auth_time: object
) -> None:
    """Checked again under the row lock: whatever auth_time Google sends."""
    browser, user_id = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    started = await start(browser, "google", "reauth")
    await oenv.run("UPDATE users SET password_hash = 'set-meanwhile' WHERE id = :u", u=user_id)
    callback = fake.complete(started.json()["authorization_url"], auth_time=auth_time)
    assert location(await browser.get(callback)) == "/account?oauth=reauth_use_password"
    await refused_unchanged(oenv, browser, old)
    assert await failures(oenv) == [{"why": "has_password", "provider": "google"}]


# ---- the binding to this flow ------------------------------------------------------------------


@pytest.mark.parametrize(("seconds", "ok"), [(290, True), (301, False)])
async def test_the_callback_must_come_within_five_minutes_of_the_start(
    oenv: Env, fake: FakeProviders, seconds: int, ok: bool
) -> None:
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    started = await start(browser, "google", "reauth")
    await oenv.run(
        "UPDATE oauth_flows SET created_at = now() - make_interval(secs => :s)", s=seconds
    )
    callback = fake.complete(started.json()["authorization_url"], auth_time=int(time.time()))
    response = await browser.get(callback)
    if ok:
        assert location(response) == REAUTHED
    else:
        assert location(response) == "/account?oauth=reauth_expired"
        await refused_unchanged(oenv, browser, old)
        assert await failures(oenv) == [{"why": "flow_old", "provider": "google"}]


async def test_an_unverified_email_is_refused(oenv: Env, fake: FakeProviders) -> None:
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    assert location(await reauth(browser, fake, email_verified=False)) == "/account?oauth=invalid"
    await refused_unchanged(oenv, browser, old)
    assert await failures(oenv) == [{"why": "email_unverified", "provider": "google"}]


async def test_a_mismatched_nonce_is_refused(oenv: Env, fake: FakeProviders) -> None:
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    assert location(await reauth(browser, fake, nonce="another-nonce")) == "/account?oauth=invalid"
    await refused_unchanged(oenv, browser, old)


async def test_a_token_replayed_into_another_flow_is_refused(
    oenv: Env, fake: FakeProviders
) -> None:
    """The first flow's ID token (its nonce) with the second flow's state and cookie."""
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    first = (await start(browser, "google", "reauth")).json()["authorization_url"]
    second = (await start(browser, "google", "reauth")).json()["authorization_url"]
    state = parse_qs(urlsplit(second).query)["state"][0]
    callback = fake.complete(first, state=state)
    assert location(await browser.get(callback)) == "/account?oauth=invalid"
    await refused_unchanged(oenv, browser, old)


async def test_a_replayed_callback_finds_no_flow(oenv: Env, fake: FakeProviders) -> None:
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    started = await start(browser, "google", "reauth")
    callback = fake.complete(started.json()["authorization_url"])
    assert location(await browser.get(callback)) == REAUTHED
    await age_reauth(oenv)
    # No flow, so no purpose: the generic sign-in page (unchanged).
    assert location(await browser.get(callback)) == "/signin?oauth=invalid"
    assert await oenv.scalar(STALE) is True


async def test_reauth_with_an_identity_of_another_user_is_refused(
    oenv: Env, fake: FakeProviders
) -> None:
    browser, _ = await google_only(oenv, fake)
    await age_reauth(oenv)
    response = await reauth(browser, fake, sub="someone-else")
    assert location(response) == "/account?oauth=identity_mismatch"
    assert await oenv.scalar(STALE) is True


async def test_reauth_needs_a_session(oenv: Env) -> None:
    assert (await start(Browser(oenv), "google", "reauth")).status_code == 401


# ---- step-up is unchanged -----------------------------------------------------------------------


async def test_a_google_reauth_is_not_a_step_up_and_a_sensitive_action_still_needs_a_code(
    oenv: Env, fake: FakeProviders
) -> None:
    """Google re-authentication, then a fresh TOTP, then an action that needs both (setting a
    first password): the 10-minute step-up and the mandatory TOTP stay in force."""
    browser, _ = await google_only(oenv, fake)
    await oenv.run(
        "UPDATE sessions SET reauth_at = now() - interval '11 minutes', "
        "stepup_at = now() - interval '11 minutes'"
    )
    act = {"new_password": "a-synthetic-new-password-1"}
    refused = await browser.post("/api/auth/password/change", act)
    assert (refused.status_code, reason(refused)) == (403, "reauth_required")

    assert location(await reauth(browser, fake)) == REAUTHED
    refused = await browser.post("/api/auth/password/change", act)
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")

    await oenv.run("UPDATE mfa_totp SET last_step = 0")  # the same step may be used again
    code = totp.code_for_step(TEST_TOTP_SECRET, int(totp.clock() // totp.PERIOD))
    assert (await browser.post("/api/auth/mfa/stepup", {"code": code})).status_code == 200
    done = await browser.post("/api/auth/password/change", act)
    assert done.status_code == 200, done.text
    assert await oenv.scalar("SELECT password_hash IS NOT NULL FROM users") is True
