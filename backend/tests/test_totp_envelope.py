"""TOTP seeds on the envelope (ADR 0003 section 5, ADR 0004): only the envelope form can be
stored; an unreadable secret fails MFA closed with the generic error, writes its own auth event
and is not an ``mfa_failed``; ``keys rewrap`` (#13) moves a seed to the current KEK. That the
seed is encrypted and its AADs bound to the user is ``test_auth_mfa.py``'s "secrets at rest"."""

import secrets
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from mfatools import admin_with_totp, clock_fixture, make_actor_fixture, step_of  # noqa: F401 - fixtures
from ownvitals.auth import totp
from ownvitals.auth.totp_keys import rewrap_totp_secrets
from ownvitals.crypto.keks import load_keyring


async def kinds(env: Env) -> list[str]:
    rows: Any = await env.scalar("SELECT array_agg(kind ORDER BY at, id) FROM auth_events")
    return list(rows or [])


async def test_a_missing_kek_is_key_unavailable_not_a_failure(
    env: Env, make_actor: Any, clock: list[float], secrets_dir: Path
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


@pytest.mark.parametrize(
    "change",
    [
        "key_id = 'app-v1', wrapped_dek = NULL, dek_nonce = NULL",  # classbeat's interim form
        "wrapped_dek = NULL",
        "dek_nonce = NULL",
        "wrapped_dek = substring(wrapped_dek from 2)",
        "nonce = nonce || '\\x00'::bytea",
        "key_id = 'pep-1'",
    ],
)
async def test_only_the_envelope_form_can_be_stored(
    env: Env, make_actor: Any, clock: list[float], change: str
) -> None:
    await admin_with_totp(env, make_actor)
    with pytest.raises(IntegrityError):
        await env.run(f"UPDATE mfa_totp SET {change}")  # noqa: S608 - the parameters above


async def test_rewrap_moves_a_seed_to_the_current_kek_and_it_still_verifies(
    env: Env, make_actor: Any, clock: list[float], secrets_dir: Path
) -> None:
    """The seed written under ``kek-1`` is rewrapped under a new current KEK once ``kek-1`` is
    retired; only the wrap changes, and the seed still verifies through the routes."""
    actor, secret, _, _ = await admin_with_totp(env, make_actor)
    before = await env.row("SELECT secret_ciphertext, nonce FROM mfa_totp")
    retired = (secrets_dir / "connection_kek").read_bytes()
    (secrets_dir / "connection_kek_kek-1").write_bytes(retired)
    (secrets_dir / "connection_kek").write_text(secrets.token_hex(32), encoding="ascii")
    settings = env.settings.model_copy(update={"connection_kek_id": "kek-2"})
    ring = await load_keyring(env.factory, settings, role="web", directory=secrets_dir)
    counts = await rewrap_totp_secrets(env.factory, ring)
    assert counts == {"kek-1 rewrapped": 1}
    after = await env.row("SELECT key_id, secret_ciphertext, nonce FROM mfa_totp")
    assert after.key_id == "kek-2"
    assert (bytes(after.secret_ciphertext), bytes(after.nonce)) == (
        bytes(before.secret_ciphertext),
        bytes(before.nonce),
    )
    assert await rewrap_totp_secrets(env.factory, ring) == {}
    env.app.state.keyring = ring
    clock[0] += 60
    code = totp.code_for_step(secret, step_of(clock[0]))
    assert (await actor.post("/api/auth/mfa/stepup", {"code": code})).status_code == 200


async def test_a_wrong_code_against_a_readable_secret_is_still_mfa_failed(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor, _, _, _ = await admin_with_totp(env, make_actor)
    assert (await actor.post("/api/auth/mfa/stepup", {"code": "000000"})).status_code == 400
    assert (await kinds(env))[-1] == "mfa_failed"
