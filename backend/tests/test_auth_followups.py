"""Follow-ups from the reviews of the M1 PRs (issue #47): session list, TOTP removal, step-up."""

from typing import Any

from sqlalchemy import event

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    menv_fixture,
    session_cookie,
    smtp_fixture,
)
from flows import Browser, reason, signed_in
from mfatools import make_actor_fixture  # noqa: F401 - fixtures
from ownvitals.auth import mfa
from ownvitals.auth.sessions import clear_mfa, list_sessions, resolve_session

SESSIONS = "/api/auth/sessions"


async def listed(env: Env, token: str) -> list[dict[str, Any]]:
    response = await env.client.get(SESSIONS, headers=session_cookie(token))
    assert response.status_code == 200
    return response.json()


# --- list_sessions leaves out sessions that are no longer valid ---------------------------------


async def test_the_session_list_leaves_out_absolute_and_idle_expired_sessions(env: Env) -> None:
    user_id = await env.user()
    mine = await env.login(user_id)
    expired = await env.login(user_id)
    idle = await env.login(user_id)
    fresh = await env.login(user_id)
    await env.run(
        "UPDATE sessions SET expires_at = now() - interval '1 second' WHERE id = :i", i=expired.id
    )
    days = env.settings.session_idle_days
    await env.run(
        "UPDATE sessions SET last_seen_at = now() - make_interval(days => :d) WHERE id = :i",
        d=days,
        i=idle.id,
    )
    ids = {item["id"] for item in await listed(env, mine.token)}
    assert ids == {str(mine.id), str(fresh.id)}
    # The rows are still there (the purge removes them); they are just not sessions any more.
    assert await env.scalar("SELECT count(*) FROM sessions") == 4


async def test_a_session_just_inside_the_idle_limit_is_still_listed(env: Env) -> None:
    user_id = await env.user()
    mine = await env.login(user_id)
    almost = await env.login(user_id)
    days = env.settings.session_idle_days
    await env.run(
        "UPDATE sessions SET last_seen_at = now() - make_interval(days => :d) + interval '1 hour'"
        " WHERE id = :i",
        d=days,
        i=almost.id,
    )
    assert {item["id"] for item in await listed(env, mine.token)} == {str(mine.id), str(almost.id)}


async def test_list_sessions_applies_the_same_rule_as_resolve_session(env: Env) -> None:
    user_id = await env.user()
    old = await env.login(user_id)
    await env.run("UPDATE sessions SET expires_at = now() - interval '1 minute'")
    async with env.db() as db:
        assert await list_sessions(db, env.settings, user_id) == []
    resolution = await resolve_session(env.factory, env.settings, old.token)
    assert resolution.outcome == "refused"


# --- TOTP removal ----------------------------------------------------------------------------


async def test_remove_totp_locks_the_totp_row_before_the_recovery_codes(env: Env) -> None:
    """The same order as regenerate_recovery_codes, so the two cannot deadlock."""
    user_id = await env.user()
    await env.run(
        "INSERT INTO mfa_totp (user_id, secret_ciphertext, nonce, key_id, wrapped_dek, "
        "dek_nonce, confirmed_at) VALUES (:u, 'x', decode(repeat('00', 12), 'hex'), 'kek-1', "
        "decode(repeat('00', 48), 'hex'), decode(repeat('00', 12), 'hex'), now())",
        u=user_id,
    )
    await env.run("INSERT INTO mfa_recovery_codes (user_id, code_hash) VALUES (:u, 'h')", u=user_id)
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        seen.append(statement.lower())

    engine = env.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with env.db() as db:
            assert await mfa.remove_totp(db, user_id) is True
    finally:
        event.remove(engine, "before_cursor_execute", record)
    deletes = [s for s in seen if s.startswith("delete from")]
    assert [s.split()[2] for s in deletes] == ["mfa_totp", "mfa_recovery_codes"]
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0
    assert await env.scalar("SELECT count(*) FROM mfa_recovery_codes") == 0


async def test_remove_totp_without_a_totp_still_clears_stray_recovery_codes(env: Env) -> None:
    user_id = await env.user()
    await env.run("INSERT INTO mfa_recovery_codes (user_id, code_hash) VALUES (:u, 'h')", u=user_id)
    async with env.db() as db:
        assert await mfa.remove_totp(db, user_id) is False
    assert await env.scalar("SELECT count(*) FROM mfa_recovery_codes") == 0


async def test_clear_mfa_also_clears_the_step_up(env: Env) -> None:
    user_id = await env.user()
    session = await env.login(user_id, mfa=True)
    await env.run("UPDATE sessions SET stepup_at = now()")
    assert await env.scalar("SELECT mfa_at IS NOT NULL AND stepup_at IS NOT NULL FROM sessions")
    async with env.db() as db:
        await clear_mfa(db, session.id)
    assert await env.scalar("SELECT mfa_at IS NULL AND stepup_at IS NULL FROM sessions") is True


async def test_removing_totp_over_http_leaves_no_step_up_on_the_session(
    env: Env, make_actor: Any
) -> None:
    from mfatools import admin_with_totp

    actor, _, _, _ = await admin_with_totp(env, make_actor)
    assert await env.scalar("SELECT stepup_at IS NOT NULL FROM sessions") is True
    response = await actor.call("DELETE", "/api/auth/mfa/totp")
    assert response.status_code == 204
    assert await env.scalar("SELECT mfa_at IS NULL AND stepup_at IS NULL FROM sessions") is True
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0


# --- email change needs the step-up of a user with a confirmed TOTP (review of #49) -------------


async def test_email_change_answers_stepup_required_for_a_user_with_confirmed_totp(
    menv: Env,
) -> None:
    browser = await signed_in(menv)
    await menv.run(
        "INSERT INTO mfa_totp (user_id, secret_ciphertext, nonce, key_id, wrapped_dek, "
        "dek_nonce, confirmed_at) SELECT id, 'x', decode(repeat('00', 12), 'hex'), 'kek-1', "
        "decode(repeat('00', 48), 'hex'), decode(repeat('00', 12), 'hex'), now() FROM users"
    )
    refused = await browser.post("/api/auth/email/change", {"new_email": "new@example.com"})
    assert (refused.status_code, reason(refused)) == (403, "stepup_required")
    assert await menv.scalar("SELECT count(*) FROM auth_tokens") == 0  # nothing was issued
    await menv.run("UPDATE sessions SET stepup_at = now()")
    allowed = await browser.post("/api/auth/email/change", {"new_email": "new@example.com"})
    assert allowed.status_code == 202


async def test_email_change_without_totp_needs_no_step_up(menv: Env) -> None:
    browser = await signed_in(menv)
    allowed = await browser.post("/api/auth/email/change", {"new_email": "new@example.com"})
    assert allowed.status_code == 202


async def test_anonymous_email_change_is_401(menv: Env) -> None:
    anonymous = Browser(menv)
    response = await anonymous.post("/api/auth/email/change", {"new_email": "n@example.com"})
    assert response.status_code == 401
