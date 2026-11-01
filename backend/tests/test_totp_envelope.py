"""TOTP on the envelope through the routes (ADR 0004 section 2): an unreadable secret fails MFA
closed with the generic error, writes its own auth event and is not an ``mfa_failed``."""

from pathlib import Path
from typing import Any

from authtools import Env
from ownvitals.auth import totp
from keystools import add_totp
from mfatools import admin_with_totp, step_of


async def kinds(env: Env) -> list[str]:
    rows: Any = await env.scalar("SELECT array_agg(kind ORDER BY at, id) FROM auth_events")
    return list(rows or [])


async def test_a_missing_kek_is_key_unavailable_not_a_failure(
    env: Env, make_actor: Any, clock: list[float], kek_dir: Path
) -> None:
    actor, secret, codes, _ = await admin_with_totp(env, make_actor)
    # The row names a retired KEK whose file is gone (an operator error).
    await env.run("UPDATE mfa_totp SET key_id = 'kek-0'")
    clock[0] += 60
    code = totp.code_for_step(secret, step_of(clock[0]))
    response = await actor.post("/api/auth/mfa/stepup", {"code": code})
    assert (response.status_code, response.json()) == (400, {"detail": {"reason": "invalid_code"}})
    assert (await kinds(env))[-1] == "totp_key_unavailable"
    assert "mfa_failed" not in await kinds(env)
    # Recovery codes do not depend on the KEK and still work.
    assert (await actor.post("/api/auth/mfa/stepup", {"code": codes[0]})).status_code == 200


async def test_a_tampered_secret_is_decrypt_failed_not_a_failure(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor, secret, _, _ = await admin_with_totp(env, make_actor)
    await env.run("UPDATE mfa_totp SET secret_ciphertext = secret_ciphertext || '\\x00'::bytea")
    clock[0] += 60
    code = totp.code_for_step(secret, step_of(clock[0]))
    assert (await actor.post("/api/auth/mfa/stepup", {"code": code})).status_code == 400
    assert (await kinds(env))[-1] == "totp_decrypt_failed"
    assert "mfa_failed" not in await kinds(env)


async def test_an_app_v1_row_still_verifies(env: Env, make_actor: Any, clock: list[float]) -> None:
    user_id = await env.user("v1@example.com", role="admin")
    await add_totp(env, env.app.state.keyring, user_id, v1=True)
    actor = await make_actor(user_id)
    from keystools import TOTP_SECRET

    clock[0] += 60
    code = totp.code_for_step(TOTP_SECRET.decode(), step_of(clock[0]))
    assert (await actor.post("/api/auth/mfa/verify", {"code": code})).status_code == 200
    assert await env.scalar("SELECT key_id FROM mfa_totp") == "app-v1"


async def test_a_wrong_code_against_a_readable_secret_is_still_mfa_failed(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor, _, _, _ = await admin_with_totp(env, make_actor)
    assert (await actor.post("/api/auth/mfa/stepup", {"code": "000000"})).status_code == 400
    assert (await kinds(env))[-1] == "mfa_failed"
