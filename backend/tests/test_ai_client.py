"""The model client's locks and budget (ADR 0014 sections 1, 5 and 7; #207): a call from web,
without consent, without the flag or over the cap is refused before anything is sent, and so is
each of the owner's locks; a call that is sent is reserved, then settled or released by the
release rule, with retries only within the reservation.

Synthetic owners and jobs, the fake model behind the real egress client; no network.
"""

import logging
from typing import Any

import pytest
from sqlalchemy import text

from fakemodel import API_KEY, CONNECT, PROTOCOL_ERROR, READ_ERROR, TIMEOUT, status
from fakemodel import text as answer
from ownvitals.ai.client import MAX_ATTEMPTS
from ownvitals.ai.gates import ModelFailed, ModelRefused
from ownvitals.ai.requests import Message, Text
from ownvitals.jobs import RunStopped
from test_ai_client_env import (  # noqa: F401 - fixtures
    PRICES,
    AiCall,
    _ai_worker,
    admin_fixture,
    ai_fixture,
    ai_settings,
    aicall_fixture,
    as_web,
    as_worker_without_ai,
    jenv_fixture,
    queue_schema_fixture,
    secrets_fixture,
)

SYNTHETIC = True


def _nothing_happened(aicall: AiCall) -> None:
    assert aicall.fake.attempts == 0
    assert aicall.reservations() == []
    assert aicall.audit() == []


class Unusable:
    """A session factory that fails the test if anything opens a database session."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a database session was opened")


# --- refused before anything is sent -----------------------------------------------------------


async def test_a_call_from_web_is_refused_before_any_socket(
    aicall: AiCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    as_web(monkeypatch)
    aicall.job.factory = Unusable()  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ModelRefused, match="outbound_blocked"):
        await aicall.call()
    _nothing_happened(aicall)


async def test_a_worker_with_ai_off_is_refused(
    aicall: AiCall, monkeypatch: pytest.MonkeyPatch
) -> None:
    as_worker_without_ai(monkeypatch)
    with pytest.raises(ModelRefused, match="outbound_blocked"):
        await aicall.call()
    _nothing_happened(aicall)


async def test_without_consent_nothing_is_sent(aicall: AiCall) -> None:
    aicall.sql("UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o", o=aicall.owner)
    with pytest.raises(ModelRefused, match="ai_not_enabled"):
        await aicall.call()
    _nothing_happened(aicall)


async def test_an_outdated_consent_sends_nothing(aicall: AiCall) -> None:
    aicall.sql(
        "UPDATE ai_consents SET text_sha256 = :h WHERE owner_id = :o", h=b"\0" * 32, o=aicall.owner
    )
    with pytest.raises(ModelRefused, match="ai_not_enabled"):
        await aicall.call()
    _nothing_happened(aicall)


async def test_without_the_flag_nothing_is_sent(aicall: AiCall) -> None:
    with pytest.raises(ModelRefused, match="ai_not_enabled"):
        await aicall.call(ai_settings(aicall.jenv.settings, ai_insights=False))
    _nothing_happened(aicall)


async def test_a_stored_flag_off_sends_nothing(aicall: AiCall) -> None:
    aicall.sql("INSERT INTO app_settings (flag, value) VALUES ('ai_insights', '\"off\"')")
    try:
        with pytest.raises(ModelRefused, match="ai_not_enabled"):
            await aicall.call()
    finally:
        aicall.sql("DELETE FROM app_settings WHERE flag = 'ai_insights'")
    _nothing_happened(aicall)


@pytest.mark.parametrize(("user_cap", "global_cap"), [(0, 0), (10, 10**12), (10**12, 10)])
async def test_over_the_cap_nothing_is_sent(aicall: AiCall, user_cap: int, global_cap: int) -> None:
    settings = ai_settings(
        aicall.jenv.settings,
        ai_user_monthly_cap_microusd=user_cap,
        ai_global_monthly_cap_microusd=global_cap,
    )
    with pytest.raises(ModelRefused, match="ai_budget_exhausted"):
        await aicall.call(settings)
    _nothing_happened(aicall)


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"ai_model": ""}, "ai_model_unset"),
        ({"ai_provider_review_ref": ""}, "ai_provider_review_missing"),
        ({"ai_provider_review_ref": "   "}, "ai_provider_review_missing"),
        ({"ai_eval_ref": ""}, "ai_eval_mismatch"),
        ({"ai_eval_ref": "synthetic-model-1/2/1"}, "ai_eval_mismatch"),  # another prompt
        ({"ai_eval_ref": "synthetic-model-1/1/1"}, "ai_eval_mismatch"),  # an older tool set
        ({"ai_eval_ref": "synthetic-other/1/1"}, "ai_eval_mismatch"),  # another model
        ({"app_secret_key": None}, "ai_not_configured"),
    ],
)
async def test_each_owner_lock_alone_sends_nothing(
    aicall: AiCall, override: dict[str, Any], code: str
) -> None:
    with pytest.raises(ModelRefused, match=code):
        await aicall.call(ai_settings(aicall.jenv.settings, **override))
    _nothing_happened(aicall)


async def test_an_unpriced_model_sends_nothing(aicall: AiCall) -> None:
    with pytest.raises(ModelRefused, match="ai_model_unpriced"):
        await aicall.call(prices={})
    _nothing_happened(aicall)


async def test_without_the_api_key_file_nothing_is_sent(aicall: AiCall) -> None:
    (aicall.secrets / "anthropic_api_key").unlink()
    with pytest.raises(ModelRefused, match="ai_not_configured"):
        await aicall.call()
    _nothing_happened(aicall)


async def test_a_cancelled_job_sends_nothing(aicall: AiCall) -> None:
    aicall.sql(
        "UPDATE jobs SET cancel_requested_at = now() WHERE id = :j", j=aicall.job.lease.job_id
    )
    with pytest.raises(ModelRefused, match="cancelled"):
        await aicall.call()
    _nothing_happened(aicall)


# --- a call that is sent -------------------------------------------------------------------------


async def test_all_locks_open_one_call_settled_at_its_cost(aicall: AiCall) -> None:
    aicall.fake.script = [answer("Synthetic answer.", input_tokens=1000, output_tokens=100)]
    reply = await aicall.call()
    assert reply.text == "Synthetic answer."
    assert len(aicall.fake.seen) == 1
    seen = aicall.fake.seen[0]
    assert seen.method == "POST"
    assert seen.url == "https://api.anthropic.com/v1/messages"
    assert seen.headers["x-api-key"] == API_KEY
    assert API_KEY.encode() not in seen.body
    cost = -(-(1000 * PRICES["synthetic-model-1"].input + 100 * 15_000_000) // 1_000_000)
    [(state, amount, actual)] = aicall.reservations()
    assert (state, actual) == ("settled", cost)
    assert amount >= cost
    [row] = aicall.audit()
    assert (row.status, row.error, row.settlement, row.cost_microusd) == (
        "completed",
        None,
        "settled",
        cost,
    )
    assert (row.input_token_count, row.output_token_count, row.attempts, row.http_status) == (
        1000,
        100,
        1,
        200,
    )
    assert row.job_id == aicall.job.lease.job_id


@pytest.mark.parametrize("step", [status(429, "1"), status(529), CONNECT])
async def test_an_unprocessed_attempt_is_retried_within_the_reservation(
    aicall: AiCall, step: Any
) -> None:
    aicall.fake.script = [step, answer()]
    await aicall.call()
    assert aicall.fake.attempts == 2
    [(state, _, _)] = aicall.reservations()  # one reservation for both attempts
    assert state == "settled"
    assert aicall.audit()[0].attempts == 2


async def test_retries_stop_after_the_limit_and_release(aicall: AiCall) -> None:
    aicall.fake.script = [status(429)] * MAX_ATTEMPTS
    with pytest.raises(ModelFailed, match="rate_limited"):
        await aicall.call()
    assert aicall.fake.attempts == MAX_ATTEMPTS
    assert [r.state for r in aicall.reservations()] == ["released"]
    [row] = aicall.audit()
    assert (row.status, row.error, row.settlement, row.http_status) == (
        "failed",
        "rate_limited",
        "released",
        429,
    )


@pytest.mark.parametrize("code", [400, 401, 403, 404, 413])
async def test_a_refusal_before_processing_is_released_without_a_retry(
    aicall: AiCall, code: int
) -> None:
    aicall.fake.script = [status(code)]
    with pytest.raises(ModelFailed, match="refused"):
        await aicall.call()
    assert aicall.fake.attempts == 1
    assert [(r.state, r.actual) for r in aicall.reservations()] == [("released", None)]


@pytest.mark.parametrize(
    ("step", "code"),
    [(status(500), "server_error"), (status(502), "server_error"), (TIMEOUT, "timeout")],
)
async def test_a_timeout_or_server_error_is_charged_at_the_reserved_amount(
    aicall: AiCall, step: Any, code: str
) -> None:
    aicall.fake.script = [step, answer()]
    with pytest.raises(ModelFailed, match=code):
        await aicall.call()
    assert aicall.fake.attempts == 1  # never retried: it may have been billed
    [(state, amount, actual)] = aicall.reservations()
    assert (state, actual) == ("settled", amount)
    assert aicall.audit()[0].settlement == "charged"


async def test_a_malformed_answer_is_charged(aicall: AiCall) -> None:
    aicall.fake.script = [(200, {"type": "message", "content": []})]
    with pytest.raises(ModelFailed, match="bad_response"):
        await aicall.call()
    [(state, amount, actual)] = aicall.reservations()
    assert (state, actual) == ("settled", amount)


async def test_a_withdrawal_between_attempts_stops_the_retry(aicall: AiCall) -> None:
    async def withdraw(_: Any) -> None:
        aicall.sql(
            "UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o", o=aicall.owner
        )

    aicall.fake.before = withdraw
    aicall.fake.script = [status(529), answer()]
    with pytest.raises(ModelFailed, match="consent"):
        await aicall.call()
    assert aicall.fake.attempts == 1
    assert [r.state for r in aicall.reservations()] == ["released"]


async def test_a_cancel_between_attempts_stops_the_retry(aicall: AiCall) -> None:
    async def cancel(_: Any) -> None:
        aicall.sql(
            "UPDATE jobs SET cancel_requested_at = now() WHERE id = :j", j=aicall.job.lease.job_id
        )

    aicall.fake.before = cancel
    aicall.fake.script = [status(429), answer()]
    with pytest.raises((ModelFailed, RunStopped)):
        await aicall.call()
    assert aicall.fake.attempts == 1
    assert [r.state for r in aicall.reservations()] == ["released"]


async def test_an_overrun_is_charged_in_full_and_recorded(aicall: AiCall) -> None:
    aicall.fake.script = [answer(input_tokens=10**7, output_tokens=10**6)]
    await aicall.call()
    [(state, amount, actual)] = aicall.reservations()
    assert state == "settled"
    assert actual > amount
    assert aicall.audit()[0].settlement == "overrun"


async def test_a_lost_lease_leaves_the_reservation_to_the_reaper(
    aicall: AiCall, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ownvitals.ai.audit")

    async def take_over(_: Any) -> None:
        aicall.sql("UPDATE jobs SET lease_version = 2 WHERE id = :j", j=aicall.job.lease.job_id)

    aicall.fake.before = take_over
    await aicall.call()
    assert [r.state for r in aicall.reservations()] == ["reserved"]
    assert aicall.audit() == []  # the attempt is no longer ours: no row, the log line only
    record = next(r for r in caplog.records if r.getMessage() == "ai call")
    assert getattr(record, "settlement", None) == "lost"


async def test_a_lease_lost_at_a_recheck_after_a_sent_attempt_is_logged(
    aicall: AiCall, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ownvitals.ai.audit")

    async def take_over(_: Any) -> None:
        aicall.sql("UPDATE jobs SET lease_version = 2 WHERE id = :j", j=aicall.job.lease.job_id)

    aicall.fake.before = take_over
    aicall.fake.script = [status(429), answer()]
    with pytest.raises(RunStopped, match="lost"):
        await aicall.call()
    assert aicall.fake.attempts == 1
    assert [r.state for r in aicall.reservations()] == ["reserved"]  # the reaper charges it
    record = next(r for r in caplog.records if r.getMessage() == "ai call")
    assert (getattr(record, "settlement", None), getattr(record, "http_status", None)) == (
        "lost",
        429,
    )


@pytest.mark.parametrize("step", [READ_ERROR, PROTOCOL_ERROR])
async def test_a_transport_error_after_the_send_is_charged_not_retried(
    aicall: AiCall, step: str
) -> None:
    aicall.fake.script = [step, answer()]
    with pytest.raises(ModelFailed, match="transport"):
        await aicall.call()
    assert aicall.fake.attempts == 1  # it may have been billed: never retried
    [(state, amount, actual)] = aicall.reservations()
    assert (state, actual) == ("settled", amount)
    [row] = aicall.audit()
    assert (row.status, row.error, row.settlement, row.cost_microusd) == (
        "failed",
        "transport",
        "charged",
        amount,
    )


async def test_the_audit_is_append_only_for_the_app_role(aicall: AiCall) -> None:
    await aicall.call()
    async with aicall.jenv.factory() as db, db.begin():
        await db.execute(
            text("SELECT set_config('app.user_id', :u, true)"), {"u": str(aicall.owner)}
        )
        assert len((await db.execute(text("SELECT id FROM ai_audit"))).all()) == 1
        with pytest.raises(Exception, match="permission denied"):
            await db.execute(text("UPDATE ai_audit SET cost_microusd = 0"))


@pytest.mark.parametrize(
    ("planted", "code"),
    [
        ("synthetic.person@example.test", "email_in_request"),
        ("4f1c2b3a-0d9e-4c8b-a7f6-5e4d3c2b1a09", "identifier_in_request"),
        (API_KEY, "secret_in_request"),
    ],
)
async def test_a_body_that_fails_the_scan_is_never_sent(
    aicall: AiCall, planted: str, code: str
) -> None:
    question = [Message("user", (Text(f"synthetic: {planted}"),))]
    with pytest.raises(ModelRefused, match=code):
        await aicall.call(messages=question)
    _nothing_happened(aicall)
