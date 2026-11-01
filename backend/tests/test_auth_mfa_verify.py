"""TOTP verification, replay, recovery codes, step-up, rate limits (ADR 0003 sections 6, 7, 9)."""

import asyncio
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
from ownvitals.auth.notices import (
    notify_mfa_failures,
    notify_totp_enrolled,
    register_security_notifier,
)

VERIFY = "/api/auth/mfa/verify"
STEPUP = "/api/auth/mfa/stepup"


def reason(response: Any) -> str:
    return response.json()["detail"]["reason"]


# --- verify at sign-in ----------------------------------------------------------------------


async def test_verify_opens_the_admin_gate_and_sets_stepup(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, secret, _, user_id = await admin_with_totp(env, make_actor)
    actor = await make_actor(user_id)  # the next sign-in: password only
    assert reason(await actor.get("/api/admin/whoami")) == "mfa_required"
    clock[0] += 60
    code = totp.code_for_step(secret, step_of(clock[0]))
    old_token = actor.token
    response = await actor.post(VERIFY, {"code": code})
    assert response.status_code == 200
    assert actor.token != old_token  # the session rotated
    assert (await actor.get("/api/admin/whoami")).status_code == 200
    assert await env.scalar(
        "SELECT stepup_at IS NOT NULL AND mfa_at IS NOT NULL "
        "FROM sessions ORDER BY created_at DESC LIMIT 1"
    )


async def test_verify_without_a_totp_is_409_and_anonymous_is_401(env: Env, make_actor: Any) -> None:
    actor = await make_actor(await env.user())
    assert (await actor.post(VERIFY, {"code": "123456"})).status_code == 409
    assert (await env.client.get("/api/auth/mfa")).status_code == 401


# --- the window and replay ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "accepted"), [(-2, False), (-1, True), (0, True), (1, True), (2, False)]
)
async def test_the_window_is_one_step_either_side(
    env: Env, make_actor: Any, clock: list[float], offset: int, accepted: bool
) -> None:
    _, secret, _, user_id = await admin_with_totp(env, make_actor)
    clock[0] += 600  # well past the enrollment step
    await env.run("UPDATE mfa_totp SET last_step = 0")
    actor = await make_actor(user_id)
    code = totp.code_for_step(secret, step_of(clock[0]) + offset)
    response = await actor.post(VERIFY, {"code": code})
    assert (response.status_code == 200) is accepted


async def test_a_code_cannot_be_replayed_nor_an_earlier_step_after_a_later_one(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, secret, _, user_id = await admin_with_totp(env, make_actor)
    clock[0] += 600
    step = step_of(clock[0])
    actor = await make_actor(user_id)
    assert (await actor.post(VERIFY, {"code": totp.code_for_step(secret, step)})).status_code == 200
    assert (await actor.post(VERIFY, {"code": totp.code_for_step(secret, step)})).status_code == 400
    older = totp.code_for_step(secret, step - 1)
    assert (await actor.post(VERIFY, {"code": older})).status_code == 400
    assert (
        await actor.post(VERIFY, {"code": totp.code_for_step(secret, step + 1)})
    ).status_code == 200


async def test_concurrent_submissions_of_one_code_let_exactly_one_win(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, secret, _, user_id = await admin_with_totp(env, make_actor)
    clock[0] += 600
    code = totp.code_for_step(secret, step_of(clock[0]))
    actors = [await make_actor(user_id) for _ in range(4)]
    results = await asyncio.gather(*(a.post(VERIFY, {"code": code}) for a in actors))
    assert sorted(r.status_code for r in results) == [200, 400, 400, 400]


# --- recovery codes -------------------------------------------------------------------------


async def test_a_recovery_code_works_once_and_in_any_notation(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, _, codes, user_id = await admin_with_totp(env, make_actor)
    actor = await make_actor(user_id)
    written = f"{codes[0][:5].lower()}-{codes[0][5:].lower()} "
    assert (await actor.post(VERIFY, {"code": written})).status_code == 200
    assert (await actor.post(VERIFY, {"code": codes[0]})).status_code == 400
    assert (await actor.get("/api/auth/mfa")).json()["recovery_codes_left"] == 9
    assert (await actor.post(VERIFY, {"code": codes[1]})).status_code == 200


async def test_concurrent_submissions_of_one_recovery_code_let_exactly_one_win(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, _, codes, user_id = await admin_with_totp(env, make_actor)
    actors = [await make_actor(user_id) for _ in range(4)]
    results = await asyncio.gather(*(a.post(VERIFY, {"code": codes[2]}) for a in actors))
    assert sorted(r.status_code for r in results) == [200, 400, 400, 400]
    assert (
        await env.scalar("SELECT count(*) FROM mfa_recovery_codes WHERE used_at IS NOT NULL") == 1
    )


async def test_another_users_recovery_code_does_not_work(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, _, codes, _ = await admin_with_totp(env, make_actor)
    _, _, _, second_id = await admin_with_totp(env, make_actor, "second@example.com")
    other = await make_actor(second_id)
    assert (await other.post(VERIFY, {"code": codes[0]})).status_code == 400


# --- step-up --------------------------------------------------------------------------------


async def test_stepup_lasts_five_minutes(env: Env, make_actor: Any, clock: list[float]) -> None:
    actor, _, _, _ = await admin_with_totp(env, make_actor)
    path = "/api/auth/mfa/recovery-codes"
    assert (await actor.post(path)).status_code == 200  # enrollment left a fresh step-up
    await env.run("UPDATE sessions SET stepup_at = now() - interval '4 minutes'")
    assert (await actor.post(path)).status_code == 200
    await env.run("UPDATE sessions SET stepup_at = now() - interval '6 minutes'")
    expired = await actor.post(path)
    assert expired.status_code == 403
    assert reason(expired) == "stepup_required"


async def test_stepup_endpoint_sets_only_the_stepup_flag(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, secret, _, user_id = await admin_with_totp(env, make_actor)
    clock[0] += 600
    actor = await make_actor(user_id)
    code = totp.code_for_step(secret, step_of(clock[0]))
    assert (await actor.post(STEPUP, {"code": code})).status_code == 200
    session = (await actor.get("/api/auth/session")).json()["session"]
    assert session["stepup"] is True
    assert session["mfa"] is False
    assert (await actor.post("/api/auth/mfa/recovery-codes")).status_code == 200


# --- TOTP as an admin re-authentication -----------------------------------------------------


async def test_an_admin_can_reauthenticate_with_one_totp_code(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, secret, _, user_id = await admin_with_totp(env, make_actor)
    clock[0] += 600
    actor = await make_actor(user_id)
    await env.run("UPDATE sessions SET reauth_at = now() - interval '1 hour', stepup_at = NULL")
    code = totp.code_for_step(secret, step_of(clock[0]))
    assert (await actor.post("/api/auth/reauth/totp", {"code": code})).status_code == 200
    assert await env.scalar(
        "SELECT reauth_at > now() - interval '1 minute' AND stepup_at IS NOT NULL "
        "AND mfa_at IS NOT NULL FROM sessions ORDER BY created_at DESC LIMIT 1"
    )


async def test_totp_is_not_a_reauth_method_for_a_plain_user(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor = await make_actor(await env.user("plain@example.com"))
    await actor.enroll()
    response = await actor.post("/api/auth/reauth/totp", {"code": "123456"})
    assert response.status_code == 403
    assert reason(response) == "forbidden"


# --- failures, events, mail and rate limits -------------------------------------------------


async def test_failures_write_events_without_the_code_and_the_fifth_mails_once(
    env: Env, make_actor: Any, clock: list[float], notifier: FakeNotifier
) -> None:
    _, _, _, user_id = await admin_with_totp(env, make_actor)
    actor = await make_actor(user_id)
    for attempt in range(1, 6):
        response = await actor.post(VERIFY, {"code": "000000"})
        assert response.status_code == 400
        assert len(notifier.failures) == (1 if attempt == 5 else 0)
    events = await env.scalar("SELECT count(*) FROM auth_events WHERE kind = 'mfa_failed'")
    assert events == 5
    detail = await env.scalar(
        "SELECT detail::text FROM auth_events WHERE kind = 'mfa_failed' LIMIT 1"
    )
    assert detail is not None
    assert "000000" not in str(detail)
    assert notifier.failures == [user_id]


async def test_the_sixth_failure_in_five_minutes_is_rate_limited(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, _, _, user_id = await admin_with_totp(env, make_actor)
    actor = await make_actor(user_id)
    for _ in range(5):
        assert (await actor.post(VERIFY, {"code": "000000"})).status_code == 400
    limited = await actor.post(VERIFY, {"code": "000000"})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
    # Even a correct recovery code is refused while the limit holds.
    assert (await actor.post(VERIFY, {"code": "ABCDEFGHIJ"})).status_code == 429


async def test_successes_do_not_count_toward_the_limit(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    _, _, codes, user_id = await admin_with_totp(env, make_actor)
    actor = await make_actor(user_id)
    for code in codes:
        assert (await actor.post(VERIFY, {"code": code})).status_code == 200


async def test_enrollment_failures_are_limited_too(
    env: Env, make_actor: Any, clock: list[float]
) -> None:
    actor = await make_actor(await env.user())
    body = (await actor.post("/api/auth/mfa/totp/start")).json()
    payload = {"pending": body["pending"], "code": "000000"}
    for _ in range(5):
        assert (await actor.post("/api/auth/mfa/totp/confirm", payload)).status_code == 400
    assert (await actor.post("/api/auth/mfa/totp/confirm", payload)).status_code == 429


# --- the mail hook --------------------------------------------------------------------------


async def test_a_missing_or_failing_notifier_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from uuid import uuid4

    register_security_notifier(None)
    await notify_totp_enrolled(uuid4(), "a@example.com")
    await notify_mfa_failures(uuid4(), "a@example.com")

    class Broken:
        async def totp_enrolled(self, user_id: Any, email: str) -> None:
            raise RuntimeError("smtp down")

        async def mfa_failures(self, user_id: Any, email: str) -> None:
            raise RuntimeError("smtp down")

    register_security_notifier(Broken())
    try:
        await notify_totp_enrolled(uuid4(), "a@example.com")
        await notify_mfa_failures(uuid4(), "a@example.com")
    finally:
        register_security_notifier(None)
