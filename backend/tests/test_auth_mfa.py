"""Admin gate, TOTP enrollment and secrets at rest (ADR 0003 sections 6 to 8, issue #39)."""

import re
import time
from typing import Any

import pytest
from fastapi import Depends

from authtools import Env
from ownvitals import auth
from ownvitals.auth import totp
from ownvitals.auth.keys import Keys
from ownvitals.auth.mfa import open_pending, seal_pending
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import DecryptFailed, open_sealed, totp_aad, totp_wrap_aad
from ownvitals.crypto.keks import KeyRing
from mfatools import FakeNotifier, admin_with_totp, step_of


def reason(response: Any) -> str:
    return response.json()["detail"]["reason"]


# --- the gate -------------------------------------------------------------------------------


async def test_anonymous_gets_401_on_whoami(env: Env) -> None:
    assert (await env.client.get("/api/admin/whoami")).status_code == 401


async def test_a_non_admin_gets_403(env: Env, make_actor: Any) -> None:
    actor = await make_actor(await env.user("plain@example.com"), mfa=True)
    response = await actor.get("/api/admin/whoami")
    assert response.status_code == 403
    assert reason(response) == "forbidden"


async def test_an_admin_without_mfa_gets_403_mfa_required(env: Env, make_actor: Any) -> None:
    actor = await make_actor(await env.user("a@example.com", role="admin"))
    response = await actor.get("/api/admin/whoami")
    assert response.status_code == 403
    assert reason(response) == "mfa_required"


async def test_an_admin_with_mfa_passes_and_the_window_is_12_hours(
    env: Env, make_actor: Any
) -> None:
    actor = await make_actor(await env.user("a@example.com", role="admin"), mfa=True)
    response = await actor.get("/api/admin/whoami")
    assert response.status_code == 200
    assert response.json()["role"] == "admin"
    await env.run("UPDATE sessions SET mfa_at = now() - interval '11 hours'")
    assert (await actor.get("/api/admin/whoami")).status_code == 200
    await env.run("UPDATE sessions SET mfa_at = now() - interval '13 hours'")
    assert reason(await actor.get("/api/admin/whoami")) == "mfa_required"


async def test_enrolling_makes_the_admin_session_pass_the_gate(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor = await make_actor(await env.user("a@example.com", role="admin"))
    await actor.enroll()
    assert (await actor.get("/api/admin/whoami")).status_code == 200


async def test_totp_is_open_to_a_plain_user_and_does_not_make_them_admin(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor = await make_actor(await env.user("plain@example.com"))
    await actor.enroll()
    assert reason(await actor.get("/api/admin/whoami")) == "forbidden"
    status = (await actor.get("/api/auth/mfa")).json()
    assert status == {"totp_enabled": True, "recovery_codes_left": 10}


# --- enrollment -----------------------------------------------------------------------------


async def test_enrollment_needs_recent_auth(env: Env, make_actor: Any) -> None:
    actor = await make_actor(await env.user())
    await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")
    response = await actor.post("/api/auth/mfa/totp/start")
    assert response.status_code == 403
    assert reason(response) == "reauth_required"
    confirm = await actor.post("/api/auth/mfa/totp/confirm", {"pending": "x", "code": "123456"})
    assert reason(confirm) == "reauth_required"
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0


async def test_enrollment_start_returns_key_and_qr_and_stores_nothing(
    env: Env, make_actor: Any
) -> None:
    actor = await make_actor(await env.user("me@example.com"))
    body = (await actor.post("/api/auth/mfa/totp/start")).json()
    assert re.fullmatch(r"[A-Z2-7]{32}", body["secret"])  # 160 bits
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    svg = body["qr_svg"]
    assert svg.startswith("<svg")
    assert re.findall(r"https?://[^\"']+", svg) == ["http://www.w3.org/2000/svg"]
    assert body["secret"] not in body["pending"]
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0


async def test_confirmation_stores_issues_codes_and_mails(
    env: Env, make_actor: Any, clock: list[float], notifier: FakeNotifier
) -> None:
    user_id = await env.user("a@example.com", role="admin")
    actor = await make_actor(user_id)
    _, codes = await actor.enroll()
    assert len(codes) == 10
    assert all(re.fullmatch(r"[A-Z2-7]{10}", c) for c in codes)
    assert notifier.enrolled == [user_id]
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'totp_enrolled'") == 1
    assert await env.scalar("SELECT mfa_at IS NOT NULL AND stepup_at IS NOT NULL FROM sessions")
    stored = await env.scalar(
        "SELECT string_agg(encode(code_hash, 'hex'), ',') FROM mfa_recovery_codes"
    )
    assert isinstance(stored, str)
    assert not any(c.lower() in stored for c in codes)  # digests only, not the codes


async def test_a_wrong_first_code_stores_nothing_and_counts_as_a_failure(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor = await make_actor(await env.user())
    body = (await actor.post("/api/auth/mfa/totp/start")).json()
    wrong = totp.code_for_step(body["secret"], step_of(clock[0]) + 5)
    response = await actor.post(
        "/api/auth/mfa/totp/confirm", {"pending": body["pending"], "code": wrong}
    )
    assert response.status_code == 400
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'mfa_failed'") == 1


async def test_a_pending_blob_of_another_user_or_a_forged_one_is_refused(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    alice = await make_actor(await env.user("alice@example.com"))
    mallory = await make_actor(await env.user("mallory@example.com"))
    body = (await alice.post("/api/auth/mfa/totp/start")).json()
    code = totp.code_for_step(body["secret"], step_of(clock[0]))
    stolen = await mallory.post(
        "/api/auth/mfa/totp/confirm", {"pending": body["pending"], "code": code}
    )
    assert stolen.status_code == 400
    forged = await mallory.post("/api/auth/mfa/totp/confirm", {"pending": "AAAA", "code": code})
    assert forged.status_code == 400
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0


async def test_the_pending_blob_expires(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    keys = Keys.from_secret(b"k" * 48)
    user_id = await env.user()
    sealed = seal_pending(keys, user_id, "SECRET", "-")
    assert open_pending(keys, user_id, sealed) == ("SECRET", "-")
    real = time.time()
    monkeypatch.setattr("ownvitals.auth.mfa.time.time", lambda: real + 601)
    assert open_pending(keys, user_id, sealed) is None


async def test_replacing_needs_stepup_and_swaps_the_secret_and_codes(
    env: Env, make_actor: Any, clock: list[float], notifier: FakeNotifier
) -> None:
    actor, old_secret, old_codes, user_id = await admin_with_totp(env, make_actor)
    await env.run("UPDATE sessions SET stepup_at = now() - interval '6 minutes'")
    refused = await actor.post("/api/auth/mfa/totp/start")
    assert refused.status_code == 403
    assert reason(refused) == "stepup_required"
    assert (await actor.post("/api/auth/mfa/stepup", {"code": old_codes[0]})).status_code == 200
    clock[0] += 60
    new_secret, new_codes = await actor.enroll()
    assert new_secret != old_secret
    assert notifier.enrolled == [user_id, user_id]
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'totp_replaced'") == 1
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 1
    other = await make_actor(user_id)
    clock[0] += 60
    old_code = totp.code_for_step(old_secret, step_of(clock[0]))
    assert (await other.post("/api/auth/mfa/stepup", {"code": old_code})).status_code == 400
    assert (await other.post("/api/auth/mfa/stepup", {"code": old_codes[1]})).status_code == 400
    assert (await other.post("/api/auth/mfa/stepup", {"code": new_codes[0]})).status_code == 200


# --- password-only re-auth cannot touch TOTP -----------------------------------------------


async def test_password_only_reauth_cannot_remove_replace_or_regenerate(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, _, _, user_id = await admin_with_totp(env, make_actor)
    other = await make_actor(user_id)  # a new sign-in: reauth_at is fresh, no step-up
    for method, path in (
        ("DELETE", "/api/auth/mfa/totp"),
        ("POST", "/api/auth/mfa/totp/start"),
        ("POST", "/api/auth/mfa/recovery-codes"),
    ):
        response = await other.call(method, path)
        assert response.status_code == 403, path
        assert reason(response) == "stepup_required", path
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 1


async def test_every_recent_auth_action_also_needs_stepup_once_totp_is_confirmed(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    @env.app.post("/api/test/sensitive")
    async def sensitive(  # pyright: ignore[reportUnusedFunction]
        ctx: UserContext = Depends(auth.require_recent_auth),  # noqa: B008
    ) -> dict[str, bool]:
        return {"ok": True}

    plain = await make_actor(await env.user("plain@example.com"))
    assert (await plain.post("/api/test/sensitive")).status_code == 200
    await plain.enroll()
    await env.run("UPDATE sessions SET stepup_at = NULL")
    assert reason(await plain.post("/api/test/sensitive")) == "stepup_required"
    await env.run("UPDATE sessions SET stepup_at = now()")
    assert (await plain.post("/api/test/sensitive")).status_code == 200
    await env.run("UPDATE sessions SET reauth_at = now() - interval '11 minutes'")
    assert reason(await plain.post("/api/test/sensitive")) == "reauth_required"


async def test_removing_totp_with_stepup(env: Env, make_actor: Any, clock: list[float]) -> None:
    actor, _, _, _ = await admin_with_totp(env, make_actor)
    assert (await actor.call("DELETE", "/api/auth/mfa/totp")).status_code == 204
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 0
    assert await env.scalar("SELECT count(*) FROM mfa_recovery_codes") == 0
    assert await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'totp_removed'") == 1


async def test_regenerating_recovery_codes_invalidates_the_old_ones(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor, _, old, user_id = await admin_with_totp(env, make_actor)
    fresh = (await actor.post("/api/auth/mfa/recovery-codes")).json()["recovery_codes"]
    assert len(fresh) == 10
    assert not set(fresh) & set(old)
    other = await make_actor(user_id)
    assert (await other.post("/api/auth/mfa/stepup", {"code": old[0]})).status_code == 400
    assert (await other.post("/api/auth/mfa/stepup", {"code": fresh[0]})).status_code == 200


# --- secrets at rest ------------------------------------------------------------------------


async def test_totp_secrets_are_encrypted_and_bound_to_their_user(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    # New enrollments write the v2 envelope form (ADR 0004 section 2).
    _, secret, _, user_id = await admin_with_totp(env, make_actor)
    row = await env.row(
        "SELECT secret_ciphertext, nonce, key_id, wrapped_dek, dek_nonce FROM mfa_totp"
    )
    assert row.key_id == "kek-1"
    assert len(bytes(row.nonce)) == 12
    assert len(bytes(row.wrapped_dek)) == 48
    assert len(bytes(row.dek_nonce)) == 12
    assert secret.encode() not in bytes(row.secret_ciphertext)
    ring: KeyRing = env.app.state.keyring
    async with env.db() as db:
        kek = await ring.current(db)

    def opened(user: Any) -> bytes:
        return open_sealed(
            row.secret_ciphertext,
            row.nonce,
            row.wrapped_dek,
            row.dek_nonce,
            kek,
            totp_aad(user),
            totp_wrap_aad(kek.key_id, user),
        )

    assert opened(user_id) == secret.encode()
    other_id = await env.user("other@example.com")
    with pytest.raises(DecryptFailed):
        opened(other_id)


async def test_a_row_moved_to_another_user_does_not_verify(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, secret, _, _ = await admin_with_totp(env, make_actor)
    other_id = await env.user("other@example.com")
    await env.run("UPDATE mfa_totp SET user_id = :other", other=other_id)
    victim = await make_actor(other_id)
    clock[0] += 60
    code = totp.code_for_step(secret, step_of(clock[0]))
    assert (await victim.post("/api/auth/mfa/stepup", {"code": code})).status_code == 400
