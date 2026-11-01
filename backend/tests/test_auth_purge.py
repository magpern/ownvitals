"""The hourly purge (ADR 0003 section 1), directly and through the worker."""

import asyncio

from authtools import Env
from ownvitals import auth
from ownvitals.worker.loop import run_worker


async def seed(env: Env) -> None:
    """One expired and one live row in every purged table."""
    user = await env.user("keep@example.com")
    sql = [
        # sessions: absolute-expired, idle-expired, live (with an old ip)
        "INSERT INTO sessions (user_id, token_hash, expires_at) "
        "SELECT id, 'a', now() - interval '1 day' FROM users",
        "INSERT INTO sessions (user_id, token_hash, expires_at, last_seen_at) "
        "SELECT id, 'b', now() + interval '1 day', now() - interval '8 days' FROM users",
        "INSERT INTO sessions (user_id, token_hash, expires_at, ip, created_at) "
        "SELECT id, 'c', now() + interval '1 day', '203.0.113.1', now() - interval '31 days' "
        "FROM users",
        "INSERT INTO pending_signups (expires_at) VALUES (now() - interval '1 minute')",
        "INSERT INTO pending_signups (expires_at) VALUES (now() + interval '1 hour')",
        "INSERT INTO auth_tokens (purpose, token_hash, user_id, expires_at) "
        "SELECT 'reset_password', 'e', id, now() - interval '1 minute' FROM users",
        "INSERT INTO auth_tokens (purpose, token_hash, user_id, expires_at, used_at) "
        "SELECT 'reset_password', 'u', id, now() + interval '1 hour', now() FROM users",
        "INSERT INTO auth_tokens (purpose, token_hash, user_id, expires_at) "
        "SELECT 'change_email', 'l', id, now() + interval '1 hour' FROM users",
        "INSERT INTO oauth_flows (flow_hash, provider, purpose, state_hash, pkce_verifier, "
        "expires_at) VALUES ('f1', 'google', 'sign_in', 's', 'v', now() - interval '1 minute')",
        "INSERT INTO oauth_flows (flow_hash, provider, purpose, state_hash, pkce_verifier, "
        "expires_at) VALUES ('f2', 'google', 'sign_in', 's', 'v', now() + interval '5 minutes')",
        "INSERT INTO rate_limits VALUES ('old', now() - interval '3 hours', 1)",
        "INSERT INTO rate_limits VALUES ('new', now(), 1)",
        "INSERT INTO invites (code_hash, expires_at, used_at) "
        "VALUES ('i1', now() + interval '1 day', now() - interval '31 days')",
        "INSERT INTO invites (code_hash, expires_at) VALUES ('i2', now() - interval '31 days')",
        "INSERT INTO invites (code_hash, expires_at, used_at) "
        "VALUES ('i3', now() + interval '1 day', now() - interval '1 day')",
        "INSERT INTO invites (code_hash, expires_at) VALUES ('i4', now() - interval '1 day')",
        "INSERT INTO known_devices (user_id, device_hash, last_used_at) "
        "SELECT id, 'd1', now() - interval '91 days' FROM users",
        "INSERT INTO known_devices (user_id, device_hash, last_used_at) "
        "SELECT id, 'd2', now() - interval '89 days' FROM users",
        "INSERT INTO auth_events (kind, at) VALUES ('old', now() - interval '31 days')",
        "INSERT INTO auth_events (kind, at) VALUES ('new', now() - interval '29 days')",
    ]
    for statement in sql:
        await env.run(statement)
    assert user is not None


async def remaining(env: Env, column: str, table: str) -> set[str]:
    async with env.db() as db:
        from sqlalchemy import text

        rows = await db.execute(text(f"SELECT {column}::text FROM {table}"))  # noqa: S608
        return {row[0] for row in rows.all()}


async def test_purge_deletes_what_is_expired_or_used_and_keeps_the_rest(env: Env) -> None:
    await seed(env)
    counts = await auth.purge_expired(env.factory, env.settings)
    assert counts == {
        "sessions": 2,
        "pending_signups": 1,
        "auth_tokens": 2,
        "oauth_flows": 1,
        "rate_limits": 1,
        "invites": 2,
        "known_devices": 1,
        "auth_events": 1,
        "sessions_ip_cleared": 1,
        "spotify_oauth_flows": 0,  # planted in test_spotify_forget.py
    }
    assert await env.scalar("SELECT count(*) FROM sessions") == 1
    assert await env.scalar("SELECT ip FROM sessions") is None  # cleared after 30 days
    assert await env.scalar("SELECT purpose FROM auth_tokens") == "change_email"
    assert await env.scalar("SELECT flow_hash::text FROM oauth_flows") == "\\x6632"
    assert await env.scalar("SELECT count(*) FROM pending_signups") == 1
    assert await env.scalar("SELECT kind FROM auth_events") == "new"
    assert await env.scalar("SELECT count(*) FROM invites") == 2
    assert await env.scalar("SELECT count(*) FROM known_devices") == 1
    assert await env.scalar("SELECT count(*) FROM rate_limits") == 1
    assert await env.scalar("SELECT count(*) FROM users") == 1  # users are never purged


async def test_purge_twice_is_a_no_op(env: Env) -> None:
    await seed(env)
    await auth.purge_expired(env.factory, env.settings)
    again = await auth.purge_expired(env.factory, env.settings)
    assert set(again.values()) == {0}


async def test_the_worker_runs_the_purge_on_its_interval(env: Env) -> None:
    await seed(env)
    from authtools import make_settings

    settings = make_settings(
        env.settings.database_url.get_secret_value(),
        purge_interval_seconds=0.05,
        worker_poll_seconds=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(settings, stop))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            if await env.scalar("SELECT count(*) FROM sessions") == 1:
                break
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    assert await env.scalar("SELECT count(*) FROM sessions") == 1
    assert await env.scalar("SELECT count(*) FROM auth_events") == 1


async def test_a_failing_purge_does_not_stop_the_worker(unreachable_database_url: str) -> None:
    from authtools import make_settings

    settings = make_settings(
        unreachable_database_url, purge_interval_seconds=0.02, worker_poll_seconds=0.02
    )
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(settings, stop))
    await asyncio.sleep(0.4)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=5)
