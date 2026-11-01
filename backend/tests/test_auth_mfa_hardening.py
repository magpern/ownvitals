"""Races, lost sessions, replayed blobs, limits and log redaction for MFA (review of PR #48)."""

import asyncio
import io
import uuid
from typing import Any

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
)
from mfatools import (  # noqa: F401 - fixtures
    FakeNotifier,
    admin_with_totp,
    clock_fixture,
    make_actor_fixture,
    notifier_fixture,
    step_of,
)
from ownvitals.auth import totp
from ownvitals.auth.notices import register_security_notifier
from ownvitals.auth.ratelimit import mfa_buckets
from ownvitals.auth.sessions import SessionGone
from ownvitals.log import configure_logging

VERIFY = "/api/auth/mfa/verify"
START = "/api/auth/mfa/totp/start"
CONFIRM = "/api/auth/mfa/totp/confirm"


def reason(response: Any) -> str:
    return response.json()["detail"]["reason"]


async def start_and_code(actor: Any, clock: list[float]) -> dict[str, Any]:
    body = (await actor.post(START)).json()
    body["code"] = totp.code_for_step(body["secret"], step_of(clock[0]))
    return body


# --- a session lost between commit and rotation ---------------------------------------------


async def test_enrollment_survives_a_session_revoked_before_rotation(
    env: Env,
    make_actor: Any,
    clock: list[float],
    notifier: FakeNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await env.user("a@example.com", role="admin")
    actor = await make_actor(user_id)
    body = await start_and_code(actor, clock)

    async def gone(*_: object, **__: object) -> None:
        raise SessionGone

    monkeypatch.setattr("ownvitals.auth.mfa_routes.rotate_session", gone)
    response = await actor.post(CONFIRM, {"pending": body["pending"], "code": body["code"]})
    assert response.status_code == 401
    payload = response.json()
    assert payload["enrolled"] is True
    assert len(payload["recovery_codes"]) == 10  # the codes are not lost
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 1
    assert notifier.enrolled == [user_id]  # and the user is told


# --- concurrent first enrollments -----------------------------------------------------------


async def test_concurrent_first_enrollments_let_exactly_one_win(
    env: Env, make_actor: Any, clock: list[float], notifier: FakeNotifier
) -> None:
    user_id = await env.user("a@example.com")
    actors = [await make_actor(user_id) for _ in range(4)]
    bodies = [await start_and_code(a, clock) for a in actors]
    results = await asyncio.gather(
        *(
            a.post(CONFIRM, {"pending": b["pending"], "code": b["code"]})
            for a, b in zip(actors, bodies, strict=True)
        )
    )
    # A loser is refused either in the route (409 totp_changed: its session was resolved before the
    # winner committed) or by ``require_recent_auth`` (403 stepup_required: it was resolved after,
    # and the user then has a confirmed TOTP and the session no step-up). Which one depends on
    # timing; both write nothing, and the first is tested without a race below.
    refusals = {(409, "totp_changed"), (403, "stepup_required")}
    assert [r.status_code for r in results].count(200) == 1
    assert {(r.status_code, reason(r)) for r in results if r.status_code != 200} <= refusals
    assert len([r for r in results if r.status_code != 200]) == 3
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 1
    assert await env.scalar("SELECT count(*) FROM mfa_recovery_codes") == 10
    assert notifier.enrolled == [user_id]


async def test_a_first_enrollment_that_lost_to_another_is_refused_totp_changed(
    env: Env, make_actor: Any, clock: list[float], notifier: FakeNotifier
) -> None:
    """The race of the test above without the race: the loser's session passes
    ``require_recent_auth`` (it has a step-up) and the route finds the TOTP changed."""
    user_id = await env.user("a@example.com")
    winner, loser = [await make_actor(user_id) for _ in range(2)]
    won = await start_and_code(winner, clock)
    lost = await start_and_code(loser, clock)
    first = await winner.post(CONFIRM, {"pending": won["pending"], "code": won["code"]})
    assert first.status_code == 200
    await env.run(
        "UPDATE sessions SET stepup_at = now()"
    )  # every session, so the dependency passes
    nonce = await env.scalar("SELECT nonce FROM mfa_totp")
    second = await loser.post(CONFIRM, {"pending": lost["pending"], "code": lost["code"]})
    assert (second.status_code, reason(second)) == (409, "totp_changed")
    assert await env.scalar("SELECT nonce FROM mfa_totp") == nonce
    assert await env.scalar("SELECT count(*) FROM mfa_totp") == 1
    assert await env.scalar("SELECT count(*) FROM mfa_recovery_codes") == 10
    assert notifier.enrolled == [user_id]


async def test_confirming_a_replacement_needs_stepup(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor, _, _, _ = await admin_with_totp(env, make_actor)
    body = await start_and_code(actor, clock)
    await env.run("UPDATE sessions SET stepup_at = now() - interval '6 minutes'")
    before = await env.scalar("SELECT nonce FROM mfa_totp")
    response = await actor.post(CONFIRM, {"pending": body["pending"], "code": body["code"]})
    assert response.status_code == 403
    assert reason(response) == "stepup_required"
    assert await env.scalar("SELECT nonce FROM mfa_totp") == before


async def test_an_old_pending_blob_cannot_be_replayed_after_a_replacement(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor, _, _, _ = await admin_with_totp(env, make_actor)
    stale = await start_and_code(actor, clock)  # started against the first TOTP
    fresh = await start_and_code(actor, clock)
    clock[0] += 60
    fresh["code"] = totp.code_for_step(fresh["secret"], step_of(clock[0]))
    done = await actor.post(CONFIRM, {"pending": fresh["pending"], "code": fresh["code"]})
    assert done.status_code == 200
    kept = await env.scalar("SELECT nonce FROM mfa_totp")
    clock[0] += 60
    code = totp.code_for_step(stale["secret"], step_of(clock[0]))
    replay = await actor.post(CONFIRM, {"pending": stale["pending"], "code": code})
    assert replay.status_code == 409
    assert await env.scalar("SELECT nonce FROM mfa_totp") == kept


async def test_a_first_enrollment_blob_cannot_be_submitted_twice(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor = await make_actor(await env.user())
    body = await start_and_code(actor, clock)
    payload = {"pending": body["pending"], "code": body["code"]}
    assert (await actor.post(CONFIRM, payload)).status_code == 200
    assert (await actor.post(CONFIRM, payload)).status_code == 409


# --- removal, regeneration, failure mail ----------------------------------------------------


async def test_removing_totp_clears_mfa_on_this_session_and_revokes_the_others(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor, _, _, user_id = await admin_with_totp(env, make_actor)
    other = await make_actor(user_id, mfa=True)
    assert (await actor.call("DELETE", "/api/auth/mfa/totp")).status_code == 204
    assert (await other.get("/api/auth/session")).json()["authenticated"] is False
    assert (await actor.get("/api/auth/session")).json()["authenticated"] is True
    assert reason(await actor.get("/api/admin/whoami")) == "mfa_required"


async def test_concurrent_regenerations_leave_exactly_ten_codes(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, _, _, user_id = await admin_with_totp(env, make_actor)
    actors = [await make_actor(user_id) for _ in range(4)]
    await env.run("UPDATE sessions SET stepup_at = now()")
    results = await asyncio.gather(*(a.post("/api/auth/mfa/recovery-codes") for a in actors))
    assert [r.status_code for r in results] == [200] * 4
    assert await env.scalar("SELECT count(*) FROM mfa_recovery_codes") == 10


async def test_concurrent_failures_send_exactly_one_failure_mail(
    env: Env, make_actor: Any, clock: list[float], notifier: FakeNotifier
) -> None:
    _, _, _, user_id = await admin_with_totp(env, make_actor)
    actors = [await make_actor(user_id) for _ in range(5)]
    results = await asyncio.gather(*(a.post(VERIFY, {"code": "000000"}) for a in actors))
    assert [r.status_code for r in results] == [400] * 5
    assert notifier.failures == [user_id]


# --- limits and rotation --------------------------------------------------------------------


async def test_mfa_is_limited_per_account_across_ips_and_per_ip_across_accounts(
    env: Env,
) -> None:
    limiter = env.app.state.limiter
    account = str(uuid.uuid4())
    for n in range(20):
        attempt = await limiter.begin(mfa_buckets(account, f"203.0.113.{n + 1}"))
        assert attempt.allowed
    assert not (await limiter.begin(mfa_buckets(account, "203.0.113.99"))).allowed
    for _ in range(20):
        assert (await limiter.begin(mfa_buckets(str(uuid.uuid4()), "198.51.100.7"))).allowed
    refused = await limiter.begin(mfa_buckets(str(uuid.uuid4()), "198.51.100.7"))
    assert not refused.allowed
    assert refused.retry_after > 0


async def test_mfa_rotation_keeps_expires_at(env: Env, make_actor: Any, clock: list[float]) -> None:
    _, secret, _, user_id = await admin_with_totp(env, make_actor)
    actor = await make_actor(user_id)
    sql = "SELECT expires_at FROM sessions ORDER BY created_at DESC LIMIT 1"
    before = await env.scalar(sql)
    clock[0] += 600
    code = totp.code_for_step(secret, step_of(clock[0]))
    assert (await actor.post(VERIFY, {"code": code})).status_code == 200
    assert await env.scalar(sql) == before


# --- T7: nothing secret reaches the logs ----------------------------------------------------


@pytest.mark.usefixtures("restore_logging")
async def test_totp_material_never_reaches_the_logs(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", stream)

    class Leaky:
        async def totp_enrolled(self, user_id: Any, email: str) -> None:
            raise RuntimeError(f"smtp failure for {email}")

        async def mfa_failures(self, user_id: Any, email: str) -> None:
            raise RuntimeError(f"smtp failure for {email}")

    register_security_notifier(Leaky())
    try:
        actor = await make_actor(await env.user("planted.address@example.com"))
        body = await start_and_code(actor, clock)
        done = await actor.post(CONFIRM, {"pending": body["pending"], "code": body["code"]})
        codes = done.json()["recovery_codes"]
        for _ in range(5):
            await actor.post(VERIFY, {"code": "000000"})
        await actor.post(VERIFY, {"code": codes[0]})
    finally:
        register_security_notifier(None)
    out = stream.getvalue()
    assert "request" in out  # the access log ran
    planted = [body["secret"], body["otpauth_uri"], body["pending"], body["code"], *codes]
    assert [p for p in planted if p in out] == []
    assert "planted.address@example.com" not in out
    stored = bytes(await env.scalar("SELECT secret_ciphertext FROM mfa_totp"))  # pyright: ignore[reportArgumentType]
    assert stored.hex() not in out
