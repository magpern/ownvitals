"""Linking, unlinking and OAuth re-authentication (ADR 0003 sections 5 and 6)."""

import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture, smtp_fixture  # noqa: F401 - fixtures
from flows import PASSWORD, Browser, age_reauth, pass_second_factor, reason, signed_in
from oauthtools import fake_fixture, location, make_invite, oenv_fixture, sign_in_with, start  # noqa: F401 - fixtures
from oidcfake import DROP, FakeProviders
from ownvitals.auth.cookies import OAUTH_COOKIE, SESSION_COOKIE
from smtpfake import SmtpFake

LINKED = "/account?oauth=linked"


async def link(browser: Browser, fake: FakeProviders, provider: str = "google", **kwargs):
    return await sign_in_with(browser, fake, provider, purpose="link", **kwargs)


async def test_link_needs_recent_authentication_before_the_start(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await signed_in(oenv, "me@example.com")
    await age_reauth(oenv)
    response = await start(browser, "google", "link")
    assert (response.status_code, reason(response)) == (403, "reauth_required")
    assert await oenv.scalar("SELECT count(*) FROM oauth_flows") == 0
    anonymous = await start(Browser(oenv), "google", "link")
    assert anonymous.status_code == 401


async def test_link_adds_the_identity_to_the_signed_in_user_and_mails_them(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser = await signed_in(oenv, "me@example.com")
    assert location(await link(browser, fake, sub="gsub", email="other@example.com")) == LINKED
    assert await oenv.scalar("SELECT subject FROM identities") == "gsub"
    assert await oenv.scalar("SELECT count(*) FROM users") == 1  # the provider's email is not used
    await asyncio.sleep(0.3)
    [mail] = smtp.to("me@example.com")
    assert "Google" in mail.text
    assert smtp.to("other@example.com") == []


async def test_the_email_collision_path_sign_in_to_the_account_then_link(
    oenv: Env, fake: FakeProviders
) -> None:
    await oenv.user(
        "g@example.com", password_hash=await oenv.app.state.password_hasher.hash(PASSWORD)
    )
    stranger = Browser(oenv)
    outcome = await sign_in_with(stranger, fake, "google", invite=await make_invite(oenv))
    assert location(outcome) == "/signin?oauth=account_exists"
    assert await oenv.scalar("SELECT count(*) FROM identities") == 0
    owner = Browser(oenv)
    assert (await owner.sign_in("g@example.com")).status_code == 200
    owner_id = await oenv.scalar("SELECT id FROM users")
    await pass_second_factor(oenv, owner, owner_id)  # pyright: ignore[reportArgumentType]
    assert location(await link(owner, fake)) == LINKED
    assert await oenv.scalar("SELECT count(*) FROM identities") == 1
    again = Browser(oenv)  # and now the provider signs in to it
    assert location(await sign_in_with(again, fake, "google")) == "/"
    assert again.signed_in


async def test_an_identity_on_another_user_is_refused(oenv: Env, fake: FakeProviders) -> None:
    other = await oenv.user("other@example.com")
    await oenv.run(
        "INSERT INTO identities (user_id, provider, subject) VALUES (:u, 'google', 'sub-1')",
        u=other,
    )
    browser = await signed_in(oenv, "me@example.com")
    assert location(await link(browser, fake)) == "/account?oauth=identity_taken"
    assert await oenv.scalar("SELECT count(*) FROM identities") == 1


async def test_linking_the_same_identity_twice_is_harmless(
    oenv: Env, fake: FakeProviders, smtp: SmtpFake
) -> None:
    browser = await signed_in(oenv, "me@example.com")
    await link(browser, fake)
    assert location(await link(browser, fake)) == LINKED
    assert await oenv.scalar("SELECT count(*) FROM identities") == 1
    await asyncio.sleep(0.3)
    assert len(smtp.to("me@example.com")) == 1  # only the new link mails


async def test_a_link_callback_in_another_users_or_no_session_is_refused(
    oenv: Env, fake: FakeProviders
) -> None:
    victim = await signed_in(oenv, "victim@example.com")
    attacker = await signed_in(oenv, "attacker@example.com")
    started = await start(attacker, "google", "link")  # the attacker's flow
    callback = fake.complete(started.json()["authorization_url"])
    victim.cookies[OAUTH_COOKIE] = attacker.cookies[OAUTH_COOKIE]
    assert location(await victim.get(callback)) == "/account?oauth=session_mismatch"
    assert await oenv.scalar("SELECT count(*) FROM identities") == 0
    assert fake.token_requests == 0
    # No session at all.
    started = await start(attacker, "google", "link")
    anonymous = Browser(oenv)
    anonymous.cookies[OAUTH_COOKIE] = attacker.cookies[OAUTH_COOKIE]
    callback = fake.complete(started.json()["authorization_url"])
    assert location(await anonymous.get(callback)) == "/account?oauth=session_mismatch"


# ---- unlink ---------------------------------------------------------------------------------


async def add_identity(env: Env, user_id, provider: str = "google", subject: str = "s") -> str:
    await env.run(
        "INSERT INTO identities (user_id, provider, subject) VALUES (:u, :p, :s)",
        u=user_id,
        p=provider,
        s=subject,
    )
    return str(
        await env.scalar(
            "SELECT id FROM identities WHERE provider = :p AND subject = :s", p=provider, s=subject
        )
    )


async def browser_for(env: Env, user_id) -> Browser:
    """A browser with a full session that has a fresh step-up (#11)."""
    issued = await env.full_login(user_id, stepup=True)
    browser = Browser(env)
    browser.cookies[SESSION_COOKIE] = issued.token
    return browser


async def test_unlink_removes_an_identity_of_a_user_with_a_password(oenv: Env) -> None:
    browser = await signed_in(oenv, "me@example.com")
    user = await oenv.scalar("SELECT id FROM users")
    identity = await add_identity(oenv, user)
    listing = (await browser.get("/api/auth/identities")).json()
    assert listing["has_password"] is True
    assert [i["provider"] for i in listing["identities"]] == ["google"]
    assert (await browser.request("DELETE", f"/api/auth/identities/{identity}")).status_code == 204
    assert await oenv.scalar("SELECT count(*) FROM identities") == 0


async def test_unlink_needs_recent_authentication(oenv: Env) -> None:
    browser = await signed_in(oenv, "me@example.com")
    identity = await add_identity(oenv, await oenv.scalar("SELECT id FROM users"))
    await age_reauth(oenv)
    response = await browser.request("DELETE", f"/api/auth/identities/{identity}")
    assert (response.status_code, reason(response)) == (403, "reauth_required")
    assert await oenv.scalar("SELECT count(*) FROM identities") == 1


async def test_the_last_sign_in_method_cannot_be_removed(oenv: Env) -> None:
    user = await oenv.user("me@example.com")  # no password
    only = await add_identity(oenv, user)
    browser = await browser_for(oenv, user)
    response = await browser.request("DELETE", f"/api/auth/identities/{only}")
    assert (response.status_code, reason(response)) == (409, "last_sign_in_method")
    assert await oenv.scalar("SELECT count(*) FROM identities") == 1
    second = await add_identity(oenv, user, "google", "2")
    assert (await browser.request("DELETE", f"/api/auth/identities/{second}")).status_code == 204
    again = await browser.request("DELETE", f"/api/auth/identities/{only}")
    assert again.status_code == 409


async def test_parallel_unlinks_cannot_remove_every_method(oenv: Env) -> None:
    user = await oenv.user("me@example.com")
    first = await add_identity(oenv, user, "google", "1")
    second = await add_identity(oenv, user, "google", "2")
    browser = await browser_for(oenv, user)
    token = await browser.csrf()
    results = await asyncio.gather(
        *(
            browser.request("DELETE", f"/api/auth/identities/{i}", csrf=token)
            for i in (first, second)
        )
    )
    assert sorted(r.status_code for r in results) == [204, 409]
    assert await oenv.scalar("SELECT count(*) FROM identities") == 1


@pytest.mark.parametrize("kind", ["foreign", "unknown", "malformed"])
async def test_unlinking_another_users_or_an_unknown_identity_is_a_404(
    oenv: Env, kind: str
) -> None:
    browser = await signed_in(oenv, "me@example.com")
    mine = await add_identity(oenv, await oenv.scalar("SELECT id FROM users"), "google", "mine")
    stranger = await oenv.user("stranger@example.com")
    theirs = await add_identity(oenv, stranger, "google", "theirs")
    target = {"foreign": theirs, "unknown": str(uuid4()), "malformed": "not-a-uuid"}[kind]
    response = await browser.request("DELETE", f"/api/auth/identities/{target}")
    assert response.status_code == 404
    assert await oenv.scalar("SELECT count(*) FROM identities") == 2
    assert mine  # untouched


# ---- re-authentication -------------------------------------------------------------------------


async def linked_browser(env: Env, fake: FakeProviders, provider: str = "google"):
    browser = await signed_in(env, "me@example.com")
    assert location(await link(browser, fake, provider)) == LINKED
    return browser


async def test_oidc_reauth_sets_reauth_at_and_rotates_the_session(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await linked_browser(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    started = await start(browser, "google", "reauth")
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    assert query["max_age"] == ["0"]
    assert "prompt" not in query  # Google does not document prompt=login
    # auth_time is requested explicitly: Google only adds it to the ID token when asked.
    assert json.loads(query["claims"][0]) == {"id_token": {"auth_time": {"essential": True}}}
    callback = fake.complete(started.json()["authorization_url"])
    assert location(await browser.get(callback)) == "/account?oauth=reauthed"
    assert browser.cookies[SESSION_COOKIE] != old
    assert await oenv.scalar("SELECT count(*) FROM sessions") == 1
    assert await oenv.scalar("SELECT reauth_at > now() - interval '1 minute' FROM sessions") is True
    info = (await browser.get("/api/auth/session")).json()["session"]
    assert info["recent_auth"] is True


async def test_google_without_the_claims_request_sends_no_auth_time_and_reauth_fails_closed(
    oenv: Env, fake: FakeProviders
) -> None:
    """The fake behaves like Google: no auth_time unless the claims parameter asks for it."""
    browser = await linked_browser(oenv, fake)
    await age_reauth(oenv)
    started = await start(browser, "google", "reauth")
    url = started.json()["authorization_url"]
    without = url.replace("claims=", "unrelated=")
    callback = fake.complete(without)
    assert location(await browser.get(callback)) == "/account?oauth=reauth_stale"
    assert await oenv.scalar("SELECT reauth_at < now() - interval '5 minutes' FROM sessions")


async def test_google_sign_in_and_link_do_not_ask_for_auth_time(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await signed_in(oenv, "me@example.com")
    started = await start(browser, "google", "link")
    assert "claims" not in parse_qs(urlsplit(started.json()["authorization_url"]).query)


@pytest.mark.parametrize(
    "auth_time",
    [DROP, int(time.time()) - 3600, int(time.time()) - 301, int(time.time()) + 3600],
    ids=["missing", "an hour old", "just too old", "in the future"],
)
async def test_reauth_fails_closed_without_a_fresh_auth_time(
    oenv: Env, fake: FakeProviders, auth_time: object
) -> None:
    browser = await linked_browser(oenv, fake)
    await age_reauth(oenv)
    old = browser.cookies[SESSION_COOKIE]
    response = await sign_in_with(browser, fake, "google", purpose="reauth", auth_time=auth_time)
    assert location(response) == "/account?oauth=reauth_stale"
    assert browser.cookies[SESSION_COOKIE] == old
    assert await oenv.scalar("SELECT reauth_at < now() - interval '5 minutes' FROM sessions")


async def test_reauth_with_an_identity_of_another_user_is_refused(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await linked_browser(oenv, fake)
    await age_reauth(oenv)
    response = await sign_in_with(browser, fake, "google", purpose="reauth", sub="someone-else")
    assert location(response) == "/account?oauth=identity_mismatch"
    assert await oenv.scalar("SELECT reauth_at < now() - interval '5 minutes' FROM sessions")


async def test_reauth_needs_a_session_and_a_flow_of_that_user(
    oenv: Env, fake: FakeProviders
) -> None:
    assert (await start(Browser(oenv), "google", "reauth")).status_code == 401
