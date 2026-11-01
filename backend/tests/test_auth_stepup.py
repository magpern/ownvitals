"""Step-up (#11, ownvitals ADR 0003 section 4): one test per action of PLAN section 6.

Each action is refused without a fresh second factor (403 ``step_up_required``; the actions that
also need recent authentication answer ``reauth_required`` first) and succeeds within the
10-minute window. Step-up attempts are rate limited and audited. Every account is synthetic.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import Depends

from authtools import (  # noqa: F401 - fixtures
    TEST_TOTP_SECRET,
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    smtp_fixture,
)
from flows import PASSWORD, Browser, reason, signed_in
from mfatools import FakeNotifier, notifier_fixture  # noqa: F401 - fixtures
from oauthtools import fake_fixture, oenv_fixture, start  # noqa: F401 - fixtures
from ownvitals.auth import totp
from ownvitals.auth.cookies import SESSION_COOKIE
from ownvitals.auth.deps import require_admin_stepup, require_sensitive, require_stepup
from ownvitals.context import UserContext

# PLAN section 6's step-up list, and the actions ADR 0003 section 4 adds, each with the test
# below that covers it. A new action goes here and gets its test.
PLAN_ACTIONS = {
    "connect and disconnect Withings": "test_connect_and_disconnect_need_the_exported_dependency",
    "delete account": "test_delete_account",
    "enable 2FA (replace the authenticator)": "test_replace_the_authenticator",
    "disable or reset 2FA": "test_remove_or_reset_the_authenticator",
    "regenerate recovery codes": "test_regenerate_recovery_codes",
    "change password": "test_change_password",
    "set a first password": "test_set_a_first_password",
    "change email": "test_change_email",
    "log out everywhere": "test_log_out_everywhere",
    "link Google": "test_link_google",
    "unlink Google": "test_unlink_google",
    "admin writes (5 minutes)": "test_admin_writes_need_a_step_up_within_five_minutes",
}

Act = Callable[[Browser], Awaitable[httpx.Response]]


def test_every_plan_action_has_its_test() -> None:
    names = set(globals())
    for action, test in PLAN_ACTIONS.items():
        assert test in names, action


async def no_stepup(env: Env) -> None:
    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")


async def fresh_stepup(env: Env, browser: Browser) -> None:
    """A step-up through the API, with the user's TOTP (``Env.enrol``'s secret)."""
    await env.run("UPDATE mfa_totp SET last_step = 0")  # the same step may be used again
    code = totp.code_for_step(TEST_TOTP_SECRET, int(totp.clock() // totp.PERIOD))
    response = await browser.post("/api/auth/mfa/stepup", {"code": code})
    assert response.status_code == 200, response.text


async def check_step_up(env: Env, browser: Browser, act: Act, ok: int) -> httpx.Response:
    """Refused without a step-up, done after one (within the window)."""
    await no_stepup(env)
    refused = await act(browser)
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")
    await fresh_stepup(env, browser)
    await env.run("UPDATE sessions SET stepup_at = now() - interval '9 minutes'")
    done = await act(browser)
    assert done.status_code == ok, done.text
    return done


async def check_sensitive(env: Env, browser: Browser, act: Act, ok: int) -> httpx.Response:
    """Recent authentication first (``reauth_required``), then the step-up."""
    await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")
    stale = await act(browser)
    assert (stale.status_code, reason(stale)) == (403, "reauth_required")
    reauth = await browser.post("/api/auth/reauth", {"password": PASSWORD})
    assert reauth.status_code == 200, reauth.text
    return await check_step_up(env, browser, act, ok)


async def user_id_of(env: Env) -> UUID:
    return UUID(str(await env.scalar("SELECT id FROM users")))


# --- the actions --------------------------------------------------------------------------------


async def test_connect_and_disconnect_need_the_exported_dependency(env: Env) -> None:
    """The connections routes arrive with #13 and M1; they depend on ``require_sensitive``."""

    @env.app.post("/api/test/connect")
    async def connect(  # pyright: ignore[reportUnusedFunction]
        ctx: UserContext = Depends(require_sensitive),  # noqa: B008
    ) -> dict[str, bool]:
        return {"ok": ctx is not None}

    browser = await signed_in(env)
    await check_sensitive(env, browser, lambda b: b.post("/api/test/connect"), 200)


async def test_delete_account(env: Env) -> None:
    browser = await signed_in(env)
    await check_sensitive(env, browser, lambda b: b.request("DELETE", "/api/auth/account"), 204)
    assert await env.scalar("SELECT count(*) FROM users") == 0


async def test_replace_the_authenticator(env: Env, notifier: FakeNotifier) -> None:
    browser = await signed_in(env)
    started = await check_step_up(env, browser, lambda b: b.post("/api/auth/mfa/totp/start"), 200)
    body = started.json()
    code = totp.code_for_step(body["secret"], int(totp.clock() // totp.PERIOD))
    await no_stepup(env)
    confirm = {"pending": body["pending"], "code": code}
    refused = await browser.post("/api/auth/mfa/totp/confirm", confirm)
    assert (refused.status_code, reason(refused)) == (403, "step_up_required")
    await fresh_stepup(env, browser)
    done = await browser.post("/api/auth/mfa/totp/confirm", confirm)
    assert done.status_code == 200, done.text
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'totp_replaced'") == 1


async def test_remove_or_reset_the_authenticator(env: Env) -> None:
    browser = await signed_in(env)
    await check_step_up(env, browser, lambda b: b.request("DELETE", "/api/auth/mfa/totp"), 204)
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0


async def test_regenerate_recovery_codes(env: Env) -> None:
    browser = await signed_in(env)
    done = await check_step_up(env, browser, lambda b: b.post("/api/auth/mfa/recovery-codes"), 200)
    assert len(done.json()["recovery_codes"]) == 10


async def test_change_password(env: Env) -> None:
    browser = await signed_in(env)
    body = {"new_password": "another long password"}
    await check_sensitive(env, browser, lambda b: b.post("/api/auth/password/change", body), 200)


async def test_set_a_first_password(env: Env) -> None:
    """A Google-only account sets its first password through the same route and rules."""
    browser = await signed_in(env)
    await env.run("UPDATE users SET password_hash = NULL")
    body = {"new_password": "a first long password"}
    await check_step_up(env, browser, lambda b: b.post("/api/auth/password/change", body), 200)
    assert await env.scalar("SELECT password_hash IS NOT NULL FROM users") is True


async def test_change_email(menv: Env) -> None:
    browser = await signed_in(menv)
    body = {"new_email": "new@example.com"}
    await check_sensitive(menv, browser, lambda b: b.post("/api/auth/email/change", body), 202)


async def test_log_out_everywhere(env: Env) -> None:
    browser = await signed_in(env)
    user_id = await user_id_of(env)
    other = await env.full_login(user_id)
    await check_step_up(env, browser, lambda b: b.post("/api/auth/logout-all"), 204)
    assert await env.scalar("SELECT count(*) FROM sessions") == 0
    assert other.token  # its row is gone with the rest
    assert not browser.signed_in  # this browser's cookie is cleared too
    row = await env.row("SELECT user_id, detail FROM auth_events WHERE kind = 'logout_everywhere'")
    assert (row.user_id, row.detail) == (user_id, {"sessions": 2})


async def test_link_google(oenv: Env) -> None:
    browser = await signed_in(oenv)
    await check_sensitive(oenv, browser, lambda b: start(b, "google", "link"), 200)


async def test_unlink_google(env: Env) -> None:
    browser = await signed_in(env)
    user_id = await user_id_of(env)
    await env.run(
        "INSERT INTO identities (user_id, provider, subject) VALUES (:u, 'google', 'sub-x')",
        u=user_id,
    )
    identity = await env.scalar("SELECT id FROM identities")
    await check_sensitive(
        env, browser, lambda b: b.request("DELETE", f"/api/auth/identities/{identity}"), 204
    )


async def test_admin_writes_need_a_step_up_within_five_minutes(env: Env) -> None:
    @env.app.post("/api/test/admin-write")
    async def admin_write(  # pyright: ignore[reportUnusedFunction]
        ctx: UserContext = Depends(require_admin_stepup),  # noqa: B008
    ) -> dict[str, str]:
        return {"role": ctx.role}

    browser = Browser(env)
    hashed = await env.app.state.password_hasher.hash(PASSWORD)
    user_id = await env.user("admin@example.com", role="admin", password_hash=hashed)
    assert (await browser.sign_in("admin@example.com")).status_code == 200
    from flows import pass_second_factor

    await pass_second_factor(env, browser, user_id)
    assert (await browser.post("/api/test/admin-write")).status_code == 200
    await env.run("UPDATE sessions SET stepup_at = now() - interval '6 minutes'")
    late = await browser.post("/api/test/admin-write")
    assert (late.status_code, reason(late)) == (403, "step_up_required")
    plain = await signed_in(env, "plain@example.com")
    assert reason(await plain.post("/api/test/admin-write")) == "forbidden"


# --- the window, the setting, the rate limit and the audit -------------------------------------


async def test_the_window_follows_the_setting(app_database_url: str, clean_auth: None) -> None:
    from authtools import running_env

    async with running_env(app_database_url, stepup_window_minutes=3) as env:

        @env.app.post("/api/test/stepped")
        async def stepped(  # pyright: ignore[reportUnusedFunction]
            ctx: UserContext = Depends(require_stepup),  # noqa: B008
        ) -> dict[str, bool]:
            return {"ok": ctx is not None}

        browser = await signed_in(env)
        await env.run("UPDATE sessions SET stepup_at = now() - interval '2 minutes'")
        assert (await browser.post("/api/test/stepped")).status_code == 200
        await env.run("UPDATE sessions SET stepup_at = now() - interval '4 minutes'")
        assert reason(await browser.post("/api/test/stepped")) == "step_up_required"


async def test_a_step_up_rotates_the_session_and_is_audited(env: Env) -> None:
    browser = await signed_in(env)
    before = browser.cookies[SESSION_COOKIE]
    await fresh_stepup(env, browser)
    assert browser.cookies[SESSION_COOKIE] != before
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'step_up'") == 1
    wrong = await browser.post("/api/auth/mfa/stepup", {"code": "000000"})
    assert (wrong.status_code, reason(wrong)) == (400, "invalid_code")
    detail = await env.scalar("SELECT detail FROM auth_events WHERE kind = 'mfa_failed'")
    assert detail == {"purpose": "stepup"}


async def test_step_up_attempts_are_rate_limited(env: Env, notifier: FakeNotifier) -> None:
    browser = await signed_in(env)
    answers: list[Any] = []
    for _ in range(6):
        response = await browser.post("/api/auth/mfa/stepup", {"code": "000000"})
        answers.append(response.status_code)
    assert answers == [400] * 5 + [429]
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'mfa_failed'") == 5


@pytest.mark.parametrize("path", ["/api/auth/mfa/stepup", "/api/auth/logout-all"])
async def test_step_up_routes_need_a_full_session(env: Env, path: str) -> None:
    user_id = await env.user()
    await env.enrol(user_id)
    browser = Browser(env)
    issued = await env.login(user_id)
    browser.cookies[SESSION_COOKIE] = issued.token
    assert reason(await browser.post(path, {"code": "000000"})) == "two_factor_required"
