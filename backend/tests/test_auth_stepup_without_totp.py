"""Step-up for an account without TOTP (#475; ADR 0003 amendment of 2026-11-12).

A user who has not set up an authenticator app steps up with a fresh first factor: the password,
a Google re-authentication (the H1 rule of #247, fail closed) or a passkey. Each sets
``reauth_at`` and ``stepup_at`` and writes a ``step_up`` row with the method; the window is
still the 10-minute step-up window. A failed or stale proof sets nothing. An account with a
TOTP, and an admin, never step up by the first factor. Every account and key is synthetic.
"""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    ADMIN_EMAIL,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    make_settings,
    running_env,
    smtp_fixture,
)
from ctx import make_context
from flows import PASSWORD, Browser, reason, signed_in, user_with_password
from oauthtools import fake_fixture, location, make_invite, oenv_fixture, sign_in_with, start  # noqa: F401 - fixtures
from oidcfake import FakeProviders
from ownvitals.auth.disconnect_job import session_stepped_up
from ownvitals.auth.stepup_first_factor import steps_up
from passkeytools import SoftAuthenticator, failed, reauth, register

STEPPED_UP = (
    "SELECT reauth_at > now() - interval '1 minute' AND stepup_at > now() - interval '1 minute'"
    " FROM sessions"
)
NO_STEPUP = "SELECT stepup_at IS NULL OR stepup_at < now() - interval '10 minutes' FROM sessions"


async def age_both(env: Env) -> None:
    """Every proof of every session older than the 10-minute windows."""
    await env.run(
        "UPDATE sessions SET reauth_at = now() - interval '11 minutes',"
        " stepup_at = now() - interval '11 minutes'"
    )


async def methods(env: Env) -> list[Any]:
    async with env.db() as db:
        rows = await db.execute(
            text("SELECT detail->>'method' FROM auth_events WHERE kind = 'step_up' ORDER BY at")
        )
        return list(rows.scalars().all())


async def plain_user(env: Env) -> Browser:
    """A user without TOTP, signed in by password: a full session (#475)."""
    browser = await signed_in(env, second_factor=False)
    assert (await browser.get("/api/auth/session")).json()["session"]["mfa"] is True
    return browser


# --- by password -------------------------------------------------------------------------------


async def test_the_password_is_the_step_up_of_an_account_without_totp(env: Env) -> None:
    browser = await plain_user(env)
    await age_both(env)
    refused = await browser.request("DELETE", "/api/auth/account")
    assert (refused.status_code, reason(refused)) == (403, "reauth_required")
    assert reason(await browser.post("/api/auth/logout-all")) == "step_up_required"
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    assert await env.scalar(STEPPED_UP) is True
    assert await methods(env) == ["password"]
    session = (await browser.get("/api/auth/session")).json()["session"]
    assert (session["recent_auth"], session["stepup"]) == (True, True)
    # Within the window both the sensitive and the step-up actions go through.
    body = {"new_password": "another long password"}
    assert (await browser.post("/api/auth/password/change", body)).status_code == 200


async def test_the_password_step_up_lasts_the_step_up_window_only(env: Env) -> None:
    browser = await plain_user(env)
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    assert reason(await browser.post("/api/auth/logout-all")) == "step_up_required"


async def test_a_wrong_password_sets_nothing(env: Env) -> None:
    browser = await plain_user(env)
    await age_both(env)
    wrong = await browser.post("/api/auth/reauth", {"password": "not the password at all"})
    assert wrong.status_code == 401
    assert await env.scalar(NO_STEPUP) is True
    assert await methods(env) == []
    assert reason(await browser.post("/api/auth/logout-all")) == "step_up_required"


async def test_the_password_is_never_the_step_up_of_an_account_with_totp(env: Env) -> None:
    """Unchanged by #475: an account with a TOTP steps up with a code only."""
    browser = await signed_in(env)  # through its TOTP
    await age_both(env)
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    assert await env.scalar(NO_STEPUP) is True
    assert reason(await browser.post("/api/auth/logout-all")) == "step_up_required"
    assert await methods(env) == []


# --- by Google ---------------------------------------------------------------------------------


async def google_only_without_totp(env: Env, fake: FakeProviders) -> Browser:
    browser = Browser(env)
    response = await sign_in_with(browser, fake, "google", invite=await make_invite(env))
    assert location(response) == "/overview"
    assert (await browser.get("/api/auth/session")).json()["session"]["mfa"] is True
    return browser


async def test_google_reauth_is_the_step_up_of_a_google_only_account_without_totp(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await google_only_without_totp(oenv, fake)
    await age_both(oenv)
    assert reason(await browser.post("/api/auth/logout-all")) == "step_up_required"
    answer = await sign_in_with(browser, fake, "google", purpose="reauth")
    assert location(answer) == "/account?oauth=reauthed"
    assert await oenv.scalar(STEPPED_UP) is True
    assert await methods(oenv) == ["google"]
    assert (await browser.post("/api/auth/logout-all")).status_code == 204


async def test_a_stale_google_reauth_is_refused_and_sets_nothing(
    oenv: Env, fake: FakeProviders
) -> None:
    """H1's 5-minute flow age (#247), fail closed: no recent authentication, no step-up."""
    browser = await google_only_without_totp(oenv, fake)
    await age_both(oenv)
    started = await start(browser, "google", "reauth")
    await oenv.run("UPDATE oauth_flows SET created_at = now() - interval '301 seconds'")
    response = await browser.get(fake.complete(started.json()["authorization_url"]))
    assert location(response) == "/account?oauth=reauth_expired"
    assert await oenv.scalar(NO_STEPUP) is True
    assert await methods(oenv) == []
    assert reason(await browser.post("/api/auth/logout-all")) == "step_up_required"


async def test_a_google_reauth_with_an_unverified_email_is_refused(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await google_only_without_totp(oenv, fake)
    await age_both(oenv)
    answer = await sign_in_with(browser, fake, "google", purpose="reauth", email_verified=False)
    assert location(answer).startswith("/account?oauth=")
    assert location(answer) != "/account?oauth=reauthed"
    assert await oenv.scalar(NO_STEPUP) is True


async def test_an_account_with_a_password_still_never_reauthenticates_through_google(
    oenv: Env, fake: FakeProviders
) -> None:
    browser = await google_only_without_totp(oenv, fake)
    await oenv.run("UPDATE users SET password_hash = 'synthetic-not-a-hash'")
    refused = await start(browser, "google", "reauth")
    assert (refused.status_code, reason(refused)) == (403, "reauth_use_password")


# --- by passkey --------------------------------------------------------------------------------


async def test_a_passkey_is_the_step_up_of_an_account_without_totp(env: Env) -> None:
    browser = await plain_user(env)
    auth = SoftAuthenticator()
    # Adding a passkey needs a recent authentication and a step-up: the password gives both.
    added = await browser.post("/api/auth/passkeys/register/options")
    assert reason(added) == "step_up_required"
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    assert (await register(browser, auth, name="laptop")).status_code == 201
    await age_both(env)
    assert reason(await browser.post("/api/auth/logout-all")) == "step_up_required"
    answer = await reauth(browser, auth)
    assert answer.status_code == 200, answer.text
    assert await env.scalar(STEPPED_UP) is True
    assert await methods(env) == ["password", "passkey"]
    assert (await browser.post("/api/auth/logout-all")).status_code == 204


async def test_a_failed_passkey_reauth_sets_nothing(env: Env) -> None:
    browser = await plain_user(env)
    assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
    auth = SoftAuthenticator()
    assert (await register(browser, auth, name="laptop")).status_code == 201
    await age_both(env)
    assert failed(await reauth(browser, auth, bad_signature=True))
    assert await env.scalar(NO_STEPUP) is True
    assert await methods(env) == ["password"]


# --- who never steps up by the first factor ----------------------------------------------------


async def test_the_rule_reads_the_accounts_totp_role_and_listed_address(env: Env) -> None:
    plain = await env.user("plain@example.com")
    with_totp = await env.user("totp@example.com")
    await env.enrol(with_totp)
    admin = await env.user("admin@example.com", role="admin")
    async with env.db() as db:
        assert await steps_up(db, env.settings, plain) is True
        assert await steps_up(db, env.settings, with_totp) is False
        assert await steps_up(db, env.settings, admin) is False
        assert await steps_up(db, env.settings, UUID(int=1)) is False  # gone: fails closed
    listed = make_settings("postgresql+psycopg://unused@127.0.0.1/x", admin_emails=ADMIN_EMAIL)
    owner = await env.user(ADMIN_EMAIL)
    async with env.db() as db:
        assert await steps_up(db, listed, owner) is False
        assert await steps_up(db, env.settings, owner) is True


@pytest.mark.parametrize("path", ["/api/auth/reauth", "/api/auth/passkeys/reauth/options"])
async def test_an_admin_without_totp_cannot_step_up_by_the_first_factor(
    env: Env, path: str
) -> None:
    await user_with_password(env, "admin@example.com", role="admin")
    browser = Browser(env)
    assert (await browser.sign_in("admin@example.com")).status_code == 200
    refused = await browser.post(path, {"password": PASSWORD})
    assert reason(refused) == "two_factor_required"
    assert await env.scalar("SELECT stepup_at IS NULL FROM sessions") is True


async def test_an_admin_with_totp_steps_up_with_its_code_only(
    app_database_url: str, clean_auth: None
) -> None:
    async with running_env(app_database_url, admin_emails=ADMIN_EMAIL) as env:
        browser = await signed_in(env, ADMIN_EMAIL)  # granted at sign-in, then its TOTP
        assert (await browser.get("/api/admin/whoami")).status_code == 200
        await age_both(env)
        assert (await browser.post("/api/auth/reauth", {"password": PASSWORD})).status_code == 200
        assert await env.scalar(NO_STEPUP) is True
        assert reason(await browser.post("/api/auth/logout-all")) == "step_up_required"


async def test_the_disconnect_job_accepts_the_first_factor_step_up_of_a_user_without_totp(
    env: Env,
) -> None:
    """The worker's re-check (``disconnect_job.session_stepped_up``) follows the same rule."""
    plain = await env.user("plain@example.com")
    with_totp = await env.user("totp@example.com")
    await env.enrol(with_totp)
    admin = await env.user("admin@example.com", role="admin")
    found: dict[str, bool] = {}
    for name, user_id in (("plain", plain), ("totp", with_totp), ("admin", admin)):
        issued = await env.login(user_id)  # mfa_at empty: a first factor only
        await env.run("UPDATE sessions SET stepup_at = now() WHERE id = :i", i=issued.id)
        found[name] = await session_stepped_up(env.factory, make_context(user_id), issued.id)
    assert found == {"plain": True, "totp": False, "admin": False}
