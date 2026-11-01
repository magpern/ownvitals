"""Sessions, cookies and the one web issuer of ``UserContext`` (ADR 0003 sections 2 and 3)."""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import Depends

from authtools import ADMIN_EMAIL, Env, session_cookie, set_cookies, unsafe_headers
from ownvitals import auth
from ownvitals.context import UserContext


@pytest.fixture
def whoami(env: Env) -> None:
    @env.app.get("/api/test/whoami")
    async def whoami(  # pyright: ignore[reportUnusedFunction]
        ctx: UserContext = Depends(auth.require_user),  # noqa: B008
    ) -> dict[str, str]:
        return {"user_id": str(ctx.user_id), "role": ctx.role}


async def test_a_valid_session_becomes_a_user_context(env: Env, whoami: None) -> None:
    user_id = await env.user()
    issued = await env.login(user_id)
    response = await env.client.get("/api/test/whoami", headers=session_cookie(issued.token))
    assert response.status_code == 200
    assert response.json() == {"user_id": str(user_id), "role": "user"}


async def test_anonymous_gets_401_without_touching_the_cookie(env: Env, whoami: None) -> None:
    response = await env.client.get("/api/test/whoami")
    assert response.status_code == 401
    assert response.json()["detail"] == {"reason": "not_authenticated"}
    assert set_cookies(response) == []


async def test_an_unknown_token_is_anonymous_and_never_clears_the_cookie(
    env: Env, whoami: None
) -> None:
    response = await env.client.get("/api/test/whoami", headers=session_cookie("never-issued"))
    assert response.status_code == 401
    assert set_cookies(response) == []
    session = await env.client.get("/api/auth/session", headers=session_cookie("never-issued"))
    assert session.json()["authenticated"] is False
    assert not any(auth.SESSION_COOKIE in c for c in set_cookies(session))


async def test_idle_expiry_refuses_deletes_and_clears_the_cookie(env: Env, whoami: None) -> None:
    issued = await env.login(await env.user())
    await env.run("UPDATE sessions SET last_seen_at = now() - make_interval(days => :days)", days=8)
    response = await env.client.get("/api/test/whoami", headers=session_cookie(issued.token))
    assert response.status_code == 401
    cleared = [c for c in set_cookies(response) if c.startswith(auth.SESSION_COOKIE)]
    assert cleared
    assert "Max-Age=0" in cleared[0]
    assert await env.scalar("SELECT count(*) FROM sessions") == 0


async def test_activity_inside_the_idle_limit_keeps_the_session(env: Env, whoami: None) -> None:
    issued = await env.login(await env.user())
    await env.run("UPDATE sessions SET last_seen_at = now() - make_interval(days => :days)", days=6)
    response = await env.client.get("/api/test/whoami", headers=session_cookie(issued.token))
    assert response.status_code == 200
    # last_seen_at moved forward (at most once a minute).
    assert await env.scalar("SELECT now() - last_seen_at < interval '1 minute' FROM sessions")


async def test_last_seen_is_updated_at_most_once_a_minute(env: Env, whoami: None) -> None:
    issued = await env.login(await env.user())
    before = await env.scalar("SELECT last_seen_at FROM sessions")
    await env.client.get("/api/test/whoami", headers=session_cookie(issued.token))
    assert await env.scalar("SELECT last_seen_at FROM sessions") == before


async def test_absolute_expiry_refuses_even_an_active_session(env: Env, whoami: None) -> None:
    issued = await env.login(await env.user())
    await env.run("UPDATE sessions SET expires_at = now() - interval '1 second'")
    response = await env.client.get("/api/test/whoami", headers=session_cookie(issued.token))
    assert response.status_code == 401
    assert any(c.startswith(auth.SESSION_COOKIE) for c in set_cookies(response))
    assert await env.scalar("SELECT count(*) FROM sessions") == 0


async def test_a_disabled_user_is_refused(env: Env, whoami: None) -> None:
    user_id = await env.user()
    issued = await env.login(user_id)
    await env.run("UPDATE users SET disabled_at = now()")
    response = await env.client.get("/api/test/whoami", headers=session_cookie(issued.token))
    assert response.status_code == 401
    assert any(c.startswith(auth.SESSION_COOKIE) for c in set_cookies(response))


async def test_the_session_has_the_limits_and_the_cookie_lifetime_of_the_adr(env: Env) -> None:
    issued = await env.login(await env.user())
    assert abs(issued.max_age - 30 * 24 * 3600) < 5
    async with env.db() as db:
        issued2 = await auth.start_session(
            db, env.settings, await env.user("b@example.com"), user_agent="x" * 500, ip=None
        )
    assert issued2.token != issued.token
    length = await env.scalar("SELECT max(length(user_agent)) FROM sessions")
    assert length == 200


async def test_only_the_hash_of_the_token_is_stored(env: Env) -> None:
    issued = await env.login(await env.user())
    stored = await env.scalar("SELECT token_hash FROM sessions")
    assert stored == auth.token_hash(issued.token)
    assert issued.token.encode() not in bytes(stored)  # pyright: ignore[reportArgumentType]


async def test_cookie_attributes(env: Env) -> None:
    from fastapi import Response

    issued = await env.login(await env.user())
    response = Response()
    auth.set_session_cookie(response, issued)
    header = response.headers["set-cookie"]
    assert header.startswith(f"{auth.SESSION_COOKIE}={issued.token}")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", f"Max-Age={issued.max_age}"):
        assert attribute in header
    assert "Domain" not in header
    device = Response()
    auth.set_device_cookie(device, "d")
    assert "HttpOnly" in device.headers["set-cookie"]
    assert f"Max-Age={365 * 24 * 3600}" in device.headers["set-cookie"]
    anon = (await env.client.get("/api/auth/session")).headers["set-cookie"]
    assert anon.startswith("__Host-ownvitals_csrf=")
    assert "Secure" in anon
    assert "SameSite=strict" in anon
    assert "HttpOnly" not in anon  # JavaScript reads it for the double submit


async def test_rotation_issues_a_new_token_and_keeps_expires_at(env: Env, whoami: None) -> None:
    user_id = await env.user()
    old = await env.login(user_id)
    resolution = await auth.resolve_session(env.factory, env.settings, old.token)
    assert resolution.info is not None
    await env.run("UPDATE sessions SET expires_at = expires_at - interval '10 days'")
    expires = await env.scalar("SELECT expires_at FROM sessions")
    async with env.db() as db:
        info = (await auth.resolve_session(env.factory, env.settings, old.token)).info
        assert info is not None
        new = await auth.rotate_session(db, info, mfa=True)
    assert new.token != old.token
    assert new.expires_at == expires  # never extended past the original 30 days
    assert await env.scalar("SELECT count(*) FROM sessions") == 1
    # No grace period: the old token is dead at once (and left alone, not cleared).
    stale = await env.client.get("/api/test/whoami", headers=session_cookie(old.token))
    assert stale.status_code == 401
    assert set_cookies(stale) == []
    fresh = await env.client.get("/api/test/whoami", headers=session_cookie(new.token))
    assert fresh.status_code == 200
    assert await env.scalar("SELECT mfa_at IS NOT NULL FROM sessions") is True


async def test_rotation_carries_over_reauth_and_mfa_unless_it_sets_them(env: Env) -> None:
    old = await env.login(await env.user(), mfa=True)
    await env.run("UPDATE sessions SET reauth_at = now() - interval '1 hour'")
    before = await env.scalar("SELECT reauth_at FROM sessions")
    async with env.db() as db:
        info = (await auth.resolve_session(env.factory, env.settings, old.token)).info
        assert info is not None
        await auth.rotate_session(db, info, stepup=True)
    assert await env.scalar("SELECT reauth_at FROM sessions") == before
    assert await env.scalar("SELECT mfa_at IS NOT NULL FROM sessions") is True
    assert await env.scalar("SELECT stepup_at IS NOT NULL FROM sessions") is True


async def test_two_parallel_rotations_leave_exactly_one_session(env: Env) -> None:
    old = await env.login(await env.user())
    info = (await auth.resolve_session(env.factory, env.settings, old.token)).info
    assert info is not None

    async def rotate() -> bool:
        try:
            async with env.db() as db:
                await auth.rotate_session(db, info)
        except auth.SessionGone:
            return False
        return True

    import asyncio

    results = await asyncio.gather(rotate(), rotate())
    assert sorted(results) == [False, True]
    assert await env.scalar("SELECT count(*) FROM sessions") == 1


async def test_sign_in_replaces_the_browsers_old_session(env: Env) -> None:
    user_id = await env.user()
    first = await env.login(user_id)
    await env.run("UPDATE sessions SET expires_at = expires_at - interval '20 days'")
    kept = await env.scalar("SELECT expires_at FROM sessions")
    async with env.db() as db:
        second = await auth.start_session(
            db, env.settings, user_id, user_agent="b", ip=None, replacing_token=first.token
        )
    assert second.expires_at == kept
    assert await env.scalar("SELECT count(*) FROM sessions") == 1
    # Another user signing in on the same browser starts clean, and the old session is gone.
    other = await env.user("other@example.com")
    async with env.db() as db:
        third = await auth.start_session(
            db, env.settings, other, user_agent="b", ip=None, replacing_token=second.token
        )
    assert third.expires_at > kept  # type: ignore[operator]
    assert await env.scalar("SELECT count(*) FROM sessions") == 1
    assert await env.scalar("SELECT user_id FROM sessions") == other


async def test_logout_deletes_the_row_and_clears_the_cookie(env: Env) -> None:
    issued = await env.login(await env.user())
    csrf = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()[
        "csrf_token"
    ]
    response = await env.client.post("/api/auth/logout", headers=unsafe_headers(issued.token, csrf))
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert await env.scalar("SELECT count(*) FROM sessions") == 0


async def test_logout_all_signs_out_every_session_of_the_user_only(env: Env) -> None:
    user_id = await env.user()
    other = await env.user("other@example.com")
    mine = [await env.login(user_id) for _ in range(3)]
    theirs = await env.login(other)
    csrf = (
        await env.client.get("/api/auth/session", headers=session_cookie(mine[0].token))
    ).json()["csrf_token"]
    response = await env.client.post(
        "/api/auth/logout-all", headers=unsafe_headers(mine[0].token, csrf)
    )
    assert response.status_code == 204
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert await env.scalar("SELECT count(*) FROM sessions WHERE user_id = :u", u=user_id) == 0
    still = await env.client.get("/api/auth/session", headers=session_cookie(theirs.token))
    assert still.json()["authenticated"] is True


async def test_sessions_are_listed_for_the_owner_only(env: Env) -> None:
    mine = await env.login(await env.user())
    await env.login(await env.user("other@example.com"), ip="198.51.100.9")
    response = await env.client.get("/api/auth/sessions", headers=session_cookie(mine.token))
    items = response.json()
    assert len(items) == 1
    assert items[0]["id"] == str(mine.id)
    assert items[0]["current"] is True
    assert "token" not in items[0]
    assert "hash" not in str(items[0]).lower()


async def test_another_users_session_id_is_a_404_like_an_unknown_one(env: Env) -> None:
    mine = await env.login(await env.user())
    theirs = await env.login(await env.user("other@example.com"))
    csrf = (await env.client.get("/api/auth/session", headers=session_cookie(mine.token))).json()[
        "csrf_token"
    ]
    headers = unsafe_headers(mine.token, csrf)
    foreign = await env.client.delete(f"/api/auth/sessions/{theirs.id}", headers=headers)
    unknown = await env.client.delete(f"/api/auth/sessions/{uuid.uuid4()}", headers=headers)
    garbage = await env.client.delete("/api/auth/sessions/not-a-uuid", headers=headers)
    assert (foreign.status_code, unknown.status_code, garbage.status_code) == (404, 404, 404)
    assert foreign.content == unknown.content
    assert await env.scalar("SELECT count(*) FROM sessions") == 2  # theirs is untouched


async def test_deleting_my_own_other_session_works(env: Env) -> None:
    user_id = await env.user()
    mine = await env.login(user_id)
    spare = await env.login(user_id)
    csrf = (await env.client.get("/api/auth/session", headers=session_cookie(mine.token))).json()[
        "csrf_token"
    ]
    response = await env.client.delete(
        f"/api/auth/sessions/{spare.id}", headers=unsafe_headers(mine.token, csrf)
    )
    assert response.status_code == 204
    assert await env.scalar("SELECT count(*) FROM sessions") == 1


async def test_an_admin_is_admin_only_after_totp_within_12_hours(env: Env) -> None:
    user_id = await env.user(ADMIN_EMAIL, role="admin")

    @env.app.get("/api/test/admin")
    async def admin_only(  # pyright: ignore[reportUnusedFunction]
        ctx: UserContext = Depends(auth.require_admin),  # noqa: B008
    ) -> dict[str, str]:
        return {"role": ctx.role}

    without = await env.login(user_id)
    refused = await env.client.get("/api/test/admin", headers=session_cookie(without.token))
    assert refused.status_code == 403
    assert refused.json()["detail"] == {"reason": "mfa_required"}
    with_mfa = await env.login(user_id, mfa=True)
    ok = await env.client.get("/api/test/admin", headers=session_cookie(with_mfa.token))
    assert ok.json() == {"role": "admin"}
    await env.run("UPDATE sessions SET mfa_at = now() - interval '13 hours'")
    stale = await env.client.get("/api/test/admin", headers=session_cookie(with_mfa.token))
    assert stale.json()["detail"] == {"reason": "mfa_required"}
    plain = await env.login(await env.user("plain@example.com"))
    assert (
        await env.client.get("/api/test/admin", headers=session_cookie(plain.token))
    ).status_code == 403


async def test_recent_auth_and_stepup_have_machine_readable_reasons(env: Env) -> None:
    @env.app.get("/api/test/recent")
    async def recent(  # pyright: ignore[reportUnusedFunction]
        ctx: UserContext = Depends(auth.require_recent_auth),  # noqa: B008
    ) -> dict[str, bool]:
        return {"ok": ctx is not None}

    @env.app.get("/api/test/stepup")
    async def stepup(  # pyright: ignore[reportUnusedFunction]
        ctx: UserContext = Depends(auth.require_stepup),  # noqa: B008
    ) -> dict[str, bool]:
        return {"ok": ctx is not None}

    issued = await env.login(await env.user())
    headers = session_cookie(issued.token)
    assert (await env.client.get("/api/test/recent", headers=headers)).status_code == 200
    await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")
    late = await env.client.get("/api/test/recent", headers=headers)
    assert (late.status_code, late.json()["detail"]) == (403, {"reason": "reauth_required"})
    no_stepup = await env.client.get("/api/test/stepup", headers=headers)
    assert no_stepup.json()["detail"] == {"reason": "stepup_required"}
    await env.run("UPDATE sessions SET stepup_at = now() - interval '4 minutes'")
    assert (await env.client.get("/api/test/stepup", headers=headers)).status_code == 200
    await env.run("UPDATE sessions SET stepup_at = now() - interval '6 minutes'")
    assert (await env.client.get("/api/test/stepup", headers=headers)).status_code == 403


async def test_session_endpoint_reports_the_user(env: Env) -> None:
    user_id = await env.user("Me@Example.com")
    issued = await env.login(user_id)
    body = (await env.client.get("/api/auth/session", headers=session_cookie(issued.token))).json()
    assert body["authenticated"] is True
    assert body["user"] == {"id": str(user_id), "email": "Me@Example.com", "role": "user"}
    assert body["session"]["recent_auth"] is True
    assert datetime.fromisoformat(
        body["session"]["expires_at"]
    ) > datetime.now().astimezone() + timedelta(days=29)


async def test_sign_in_inherits_nothing_from_an_old_session_that_is_no_longer_valid(
    env: Env,
) -> None:
    user_id = await env.user()
    cases = {
        "absolute": "UPDATE sessions SET expires_at = now() - interval '1 second'",
        "idle": "UPDATE sessions SET last_seen_at = now() - interval '8 days'",
        "disabled": "UPDATE users SET disabled_at = now()",
    }
    for name, breakage in cases.items():
        old = await env.login(user_id, mfa=True)
        await env.run("UPDATE sessions SET stepup_at = now()")
        await env.run(breakage)
        async with env.db() as db:
            new = await auth.start_session(
                db, env.settings, user_id, user_agent="b", ip=None, replacing_token=old.token
            )
        assert abs(new.max_age - 30 * 24 * 3600) < 5, name  # a fresh 30 days
        assert await env.scalar("SELECT count(*) FROM sessions") == 1, name  # old one gone
        assert await env.scalar("SELECT mfa_at IS NULL FROM sessions") is True, name
        assert await env.scalar("SELECT stepup_at IS NULL FROM sessions") is True, name
        await env.run("DELETE FROM sessions")
        await env.run("UPDATE users SET disabled_at = NULL")


async def test_the_session_endpoint_reports_mfa_only_inside_the_12_hour_window(
    env: Env,
) -> None:
    issued = await env.login(await env.user(), mfa=True)
    headers = session_cookie(issued.token)
    body = (await env.client.get("/api/auth/session", headers=headers)).json()
    assert body["session"]["mfa"] is True
    await env.run("UPDATE sessions SET mfa_at = now() - interval '13 hours'")
    body = (await env.client.get("/api/auth/session", headers=headers)).json()
    assert body["session"]["mfa"] is False


async def _pending(env: Env, token: str) -> object:
    body = (await env.client.get("/api/auth/session", headers=session_cookie(token))).json()
    assert body["authenticated"] is True
    return body["session"]["admin_pending_mfa"]


async def test_the_session_flags_an_admin_whose_totp_check_is_missing_or_lapsed(env: Env) -> None:
    """ADR 0011 section 6 (#330): the page can show the code prompt only if the session says the
    account is an admin whose check is missing or older than 12 hours (role stays ``user``)."""
    admin_id = await env.user("boss@example.com", role="admin")
    never = await env.login(admin_id)  # no TOTP check yet
    body = (await env.client.get("/api/auth/session", headers=session_cookie(never.token))).json()
    assert body["user"]["role"] == "user"
    assert body["session"]["admin_pending_mfa"] is True
    whoami = await env.client.get("/api/admin/whoami", headers=session_cookie(never.token))
    assert (whoami.status_code, whoami.json()["detail"]) == (403, {"reason": "mfa_required"})

    passed = await env.login(admin_id, mfa=True)
    body = (await env.client.get("/api/auth/session", headers=session_cookie(passed.token))).json()
    assert body["user"]["role"] == "admin"
    assert body["session"]["admin_pending_mfa"] is False

    await env.run("UPDATE sessions SET mfa_at = now() - interval '11 hours'")
    assert await _pending(env, passed.token) is False
    await env.run("UPDATE sessions SET mfa_at = now() - interval '13 hours'")
    assert await _pending(env, passed.token) is True  # lapsed
    whoami = await env.client.get("/api/admin/whoami", headers=session_cookie(passed.token))
    assert (whoami.status_code, whoami.json()["detail"]) == (403, {"reason": "mfa_required"})


async def test_the_pending_flag_is_never_set_for_a_non_admin_or_another_users_session(
    env: Env,
) -> None:
    """A real non-admin sees ``false`` whatever its session state, and an admin's pending check
    does not show in anyone else's session; the admin routes keep their own 403 ``forbidden``."""
    admin_id = await env.user("boss@example.com", role="admin")
    user_id = await env.user("rider@example.com")
    admin = await env.login(admin_id)
    plain = await env.login(user_id)
    plain_mfa = await env.login(user_id, mfa=True)  # a user who passed TOTP is still no admin
    assert await _pending(env, admin.token) is True
    assert await _pending(env, plain.token) is False
    assert await _pending(env, plain_mfa.token) is False
    await env.run(
        "UPDATE sessions SET mfa_at = now() - interval '13 hours' WHERE user_id = :u",
        u=user_id,
    )
    assert await _pending(env, plain_mfa.token) is False
    whoami = await env.client.get("/api/admin/whoami", headers=session_cookie(plain.token))
    assert (whoami.status_code, whoami.json()["detail"]) == (403, {"reason": "forbidden"})
    # Anonymous: no session object at all, so no flag.
    anonymous = (await env.client.get("/api/auth/session")).json()
    assert anonymous["authenticated"] is False
    assert anonymous["session"] is None


async def test_a_demoted_admin_stops_being_pending(env: Env) -> None:
    """The flag follows the stored role at request time: after ``admin revoke`` it is false."""
    admin_id = await env.user("boss@example.com", role="admin")
    issued = await env.login(admin_id)
    assert await _pending(env, issued.token) is True
    await env.run("UPDATE users SET role = 'user' WHERE id = :u", u=admin_id)
    assert await _pending(env, issued.token) is False
