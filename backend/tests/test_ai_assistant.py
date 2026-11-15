"""The chat assistant's turn in the worker (ADR 0014 sections 3, 5 and 7; PLAN section 7.4; #208):
a message posted through the API is answered by the ``ai_chat_turn`` job, run by the worker's
runner with the fake model; refusals store nothing; a withdrawal mid-turn stops it, ends the job's
event stream and deletes the history; a turn never reads or writes another user's conversation
(RLS in the worker context).

Synthetic users and text, the fake model; no network.
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from aichat import (  # noqa: F401 - fixtures
    ChatEnv,
    _ai_worker,
    chat_fixture,
    chat_secrets_fixture,
    conversation,
    count,
    messages,
    send,
    sql,
    user,
)
from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from ctx import make_context
from fakemodel import CONNECT, Seen, status, tools
from fakemodel import text as answer
from ownvitals.ai import assistant, consent
from ownvitals.ai.chat_model import CHAT_KIND
from ownvitals.db import user_session
from ownvitals.jobs import JobRun
from ownvitals.jobs.errors import JobFailure, RunStopped
from ownvitals.jobs.fence import Lease
from ownvitals.jobs.withings import lane_for

SYNTHETIC = True
# Far above a NOWAIT answer (slow runners), far below a hung lock wait.
STORE_SECONDS = 30


def _job(job_id: str) -> Any:
    return sql(
        "SELECT state, error_category, input FROM jobs WHERE id = :j", j=uuid.UUID(job_id)
    ).one()


# --- one turn ------------------------------------------------------------------------------------


async def test_a_message_is_answered_by_the_worker_and_read_through_the_api(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-chat-a@example.com")
    conv = await conversation(chat, alice)
    chat.fake.script = [answer("Synthetic answer about your synthetic weight.")]
    posted = await send(alice, conv, "How is my weight? (synthetic)")
    assert posted.status_code == 202, posted.text
    job_id = posted.json()["job_id"]
    stored = _job(job_id)
    assert stored.state == "queued"
    # The job row holds ids only, never the text (ADR 0014 section 5, T47).
    assert set(stored.input) == {"conversation_id", "message_id"}
    assert "weight" not in str(stored.input)

    execution = await chat.run_next()
    assert execution is not None
    assert execution.result == "completed"
    assert len(chat.fake.seen) == 1
    assert messages(alice.id) == [
        ("user", "How is my weight? (synthetic)"),
        ("assistant", "Synthetic answer about your synthetic weight."),
    ]
    detail = (await alice.browser.get(f"/api/ai/conversations/{conv}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["turn"] == {"job_id": job_id, "state": "completed", "failure": None}
    # The job's event stream ends with its terminal event (ids and states only).
    events = await alice.browser.get(f"/api/jobs/{job_id}/events")
    assert "event: completed" in events.text
    assert "synthetic" not in events.text
    assert count("ai_audit", alice.id) == 1


async def test_the_history_sent_is_the_conversations_own_and_in_order(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-chat-b@example.com")
    conv = await conversation(chat, alice)
    other = await conversation(chat, alice)
    for n in range(3):
        await send(alice, other, f"synthetic other conversation {n}")
        await chat.run_next()
    chat.fake.seen.clear()
    for n in range(2):
        assert (await send(alice, conv, f"synthetic question {n}")).status_code == 202
        await chat.run_next()
    sent = chat.fake.seen[-1].json["messages"]
    texts = [block["text"] for m in sent for block in m["content"]]
    assert texts[0] == "synthetic question 0"
    assert texts[-1] == "synthetic question 1"
    assert not any("other conversation" in t for t in texts)


async def test_a_tool_turn_runs_the_tool_for_the_user_and_stores_only_the_answer(
    chat: ChatEnv,
) -> None:
    alice = await user(chat, "synthetic-chat-c@example.com")
    conv = await conversation(chat, alice)
    chat.fake.script = [tools(("list_metrics", {})), answer("Synthetic summary.")]
    await send(alice, conv, "What do you have? (synthetic)")
    execution = await chat.run_next()
    assert execution is not None
    assert execution.result == "completed"
    assert len(chat.fake.seen) == 2
    assert messages(alice.id)[-1] == ("assistant", "Synthetic summary.")
    assert count("ai_messages", alice.id) == 2  # no tool result is stored


# --- refused or failed: nothing stored -----------------------------------------------------------


async def _failed_turn(chat: ChatEnv, email: str, script: list[Any], **options: Any) -> Any:
    who = await user(chat, email)
    conv = await conversation(chat, who)
    chat.fake.script = script
    posted = await send(who, conv, "synthetic question")
    execution = await chat.run_next(**options)
    assert execution is not None
    assert execution.result == "failed"
    assert messages(who.id) == [("user", "synthetic question")]
    return who, conv, _job(posted.json()["job_id"])


async def test_a_provider_failure_fails_the_turn_without_an_answer(chat: ChatEnv) -> None:
    who, conv, job = await _failed_turn(chat, "synthetic-chat-d@example.com", [status(500)])
    assert job.error_category == "ai_unavailable"
    detail = (await who.browser.get(f"/api/ai/conversations/{conv}")).json()
    assert detail["turn"]["failure"] == "ai_unavailable"


async def test_an_unknown_tool_or_too_many_tools_store_nothing(chat: ChatEnv) -> None:
    _, _, job = await _failed_turn(
        chat, "synthetic-chat-e@example.com", [tools(("delete_account", {}))]
    )
    assert job.error_category == "ai_unavailable"
    many = [tools(*[("list_metrics", {})] * 9)]
    _, _, job = await _failed_turn(chat, "synthetic-chat-f@example.com", many)
    assert job.error_category == "ai_unavailable"


async def test_a_retried_turn_is_never_a_job_retry(chat: ChatEnv) -> None:
    # Three connect failures: the client's own attempts, one reservation; the job is not retried.
    _, _, job = await _failed_turn(
        chat, "synthetic-chat-g@example.com", [CONNECT, CONNECT, CONNECT]
    )
    assert job.state == "failed"
    assert job.error_category == "ai_unavailable"


async def test_the_cap_reached_after_queueing_fails_as_budget_exhausted(chat: ChatEnv) -> None:
    capped = chat.env.settings.model_copy(update={"ai_user_monthly_cap_microusd": 1})
    who = await user(chat, "synthetic-chat-h@example.com")
    conv = await conversation(chat, who)
    posted = await send(who, conv, "synthetic question")
    execution = await chat.run_next(settings=capped)
    assert execution is not None
    assert execution.result == "failed"
    assert _job(posted.json()["job_id"]).error_category == "ai_budget_exhausted"
    assert chat.fake.attempts == 0
    assert count("ai_messages", who.id) == 1


async def test_consent_withdrawn_after_queueing_refuses_before_any_call(chat: ChatEnv) -> None:
    who = await user(chat, "synthetic-chat-i@example.com")
    conv = await conversation(chat, who)
    posted = await send(who, conv, "synthetic question")
    sql("UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o", o=who.id)
    execution = await chat.run_next()
    assert execution is not None
    assert execution.result == "failed"
    assert _job(posted.json()["job_id"]).error_category == "ai_not_enabled"
    assert chat.fake.attempts == 0


# --- withdrawal mid-stream -----------------------------------------------------------------------


async def test_withdrawal_mid_turn_stops_the_stream_and_deletes_the_history(
    chat: ChatEnv,
) -> None:
    who = await user(chat, "synthetic-chat-j@example.com")
    conv = await conversation(chat, who)
    posted = await send(who, conv, "synthetic question")
    job_id = posted.json()["job_id"]
    withdrawn: list[int] = []

    async def withdraw_now(_: Seen) -> None:  # the model is "thinking" when the user leaves
        response = await who.browser.request("DELETE", "/api/ai/consent")
        assert response.status_code == 200, response.text
        withdrawn.append(response.json()["cancelled_jobs"])

    chat.fake.before = withdraw_now
    chat.fake.script = [answer("Synthetic answer that must not be kept.")]
    execution = await chat.run_next()
    assert execution is not None
    assert withdrawn == [1]  # the running turn was asked to stop
    assert execution.result == "cancelled"
    assert count("ai_messages", who.id) == 0
    assert count("ai_conversations", who.id) == 0
    stream = await who.browser.get(f"/api/jobs/{job_id}/events")
    assert stream.status_code == 200
    assert stream.text.rstrip().split("\n\n")[-1].startswith("id: ")
    assert "event: cancel_requested" in stream.text
    assert stream.text.rstrip().endswith("data: {}")
    assert "event: cancelled" in stream.text
    # Reconnecting after the end: nothing more to send.
    last = stream.text.rstrip().split("\n\n")[-1].split("\n")[0].removeprefix("id: ")
    again = await who.browser.get(f"/api/jobs/{job_id}/events?after={last}")
    assert again.status_code == 204
    detail = await who.browser.get(f"/api/ai/conversations/{conv}")
    assert detail.status_code == 404


async def test_the_store_does_not_wait_on_a_withdrawal_that_holds_the_consent_row(
    chat: ChatEnv,
) -> None:
    """The store holds its job row (the fence) and a withdrawal locks the consent row first and
    the job rows after it: the store's ``NOWAIT`` read refuses at once instead of closing the
    cycle, and the withdrawal then completes."""
    who = await user(chat, "synthetic-chat-k@example.com")
    conv = await conversation(chat, who)
    posted = await send(who, conv, "synthetic question")
    job_id = uuid.UUID(posted.json()["job_id"])
    sql(
        "UPDATE jobs SET state = 'running', attempt = 1, lease_version = 1, "
        "leased_by = 'synthetic-worker', lease_expires_at = now() + interval '10 minutes', "
        "heartbeat_at = now(), started_at = now() WHERE id = :j",
        j=job_id,
    )
    ctx = make_context(who.id)
    job = JobRun(
        chat.worker_factory,
        Lease(job_id, 1, ctx),
        kind=CHAT_KIND,
        input=None,  # pyright: ignore[reportArgumentType]
        attempt=1,
        lease_seconds=60,
        shutdown=asyncio.Event(),
    )
    store = assistant._store(who.id, conv, "synthetic answer")  # pyright: ignore[reportPrivateUsage]
    async with user_session(chat.env.factory, ctx) as withdrawing:
        await withdrawing.execute(
            text("UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o"), {"o": who.id}
        )
        with pytest.raises(RunStopped) as refused:  # a withdrawal is a cancel, not a failure
            async with asyncio.timeout(STORE_SECONDS), job.fenced() as fs:
                await store(fs)
        assert refused.value.reason == "cancelled"
        # The withdrawal goes on: the job row is free again.
        await withdrawing.execute(
            text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"), {"j": job_id}
        )
    assert count("ai_messages", who.id) == 1  # only the question; the answer was not stored


async def test_the_store_fails_the_job_on_an_outdated_consent(chat: ChatEnv) -> None:
    """Only a withdrawal is a cancel; a consent for an older text still fails the job."""
    who = await user(chat, "synthetic-chat-m@example.com")
    conv = await conversation(chat, who)
    posted = await send(who, conv, "synthetic question")
    job_id = uuid.UUID(posted.json()["job_id"])
    sql(
        "UPDATE jobs SET state = 'running', attempt = 1, lease_version = 1, "
        "leased_by = 'synthetic-worker', lease_expires_at = now() + interval '10 minutes', "
        "heartbeat_at = now(), started_at = now() WHERE id = :j",
        j=job_id,
    )
    sql(
        "UPDATE ai_consents SET consent_version = consent_version + 1 WHERE owner_id = :o", o=who.id
    )
    job = JobRun(
        chat.worker_factory,
        Lease(job_id, 1, make_context(who.id)),
        kind=CHAT_KIND,
        input=None,  # pyright: ignore[reportArgumentType]
        attempt=1,
        lease_seconds=60,
        shutdown=asyncio.Event(),
    )
    store = assistant._store(who.id, conv, "synthetic answer")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(JobFailure, match="ai_not_enabled"):
        async with job.fenced() as fs:
            await store(fs)
    assert count("ai_messages", who.id) == 1


async def test_without_a_withdrawal_the_store_shares_the_consent_row(chat: ChatEnv) -> None:
    who = await user(chat, "synthetic-chat-l@example.com")
    owner = who.id
    async with user_session(chat.env.factory, make_context(owner)) as db:
        await consent.require_consent(db, owner, nowait=True)
        async with user_session(chat.env.factory, make_context(owner)) as other:
            await consent.require_consent(other, owner, nowait=True)  # FOR SHARE twice is fine


# --- isolation in the worker context -------------------------------------------------------------


async def test_a_turn_of_one_user_never_reads_or_answers_anothers_conversation(
    chat: ChatEnv,
) -> None:
    alice = await user(chat, "synthetic-chat-m@example.com")
    bob = await user(chat, "synthetic-chat-n@example.com")
    conv = await conversation(chat, alice)
    await send(alice, conv, "synthetic secret question of alice")
    await chat.run_next()
    before = messages(alice.id)
    alices_message = sql(
        "SELECT id FROM ai_messages WHERE owner_id = :o AND role = 'user'", o=alice.id
    ).scalar_one()
    # A job of Bob that names Alice's conversation and message (as a crafted row could).
    sql(
        "INSERT INTO jobs (owner_id, kind, origin, lane, state, input, idempotency_key, "
        "request_hash, next_seq) VALUES (:o, :k, 'user', :lane, 'queued', CAST(:i AS jsonb), "
        ":key, :h, 1)",
        o=bob.id,
        lane=lane_for(CHAT_KIND, "user"),
        k=CHAT_KIND,
        i=f'{{"conversation_id": "{conv}", "message_id": "{alices_message}"}}',
        key="synthetic-crafted-" + uuid.uuid4().hex[:20],
        h=b"\0" * 32,
    )
    chat.fake.seen.clear()
    execution = await chat.run_next()
    assert execution is not None
    assert execution.result == "failed"
    assert chat.fake.attempts == 1  # Alice's own turn above; nothing for Bob's job
    assert chat.fake.seen == []
    assert messages(alice.id) == before
    assert count("ai_messages", bob.id) == 0
    failed = sql("SELECT error_category FROM jobs WHERE owner_id = :o", o=bob.id).scalar_one()
    assert failed == "invalid_input"


async def _crafted_turn(owner: uuid.UUID, conversation_id: uuid.UUID, message_id: Any) -> None:
    """A queued turn of ``owner`` naming ``message_id`` (as a stale or crafted job could)."""
    sql(
        "INSERT INTO jobs (owner_id, kind, origin, lane, state, input, idempotency_key, "
        "request_hash, next_seq) VALUES (:o, :k, 'user', :lane, 'queued', CAST(:i AS jsonb), "
        ":key, :h, 1)",
        o=owner,
        k=CHAT_KIND,
        lane=lane_for(CHAT_KIND, "user"),
        i=f'{{"conversation_id": "{conversation_id}", "message_id": "{message_id}"}}',
        key="synthetic-crafted-" + uuid.uuid4().hex[:20],
        h=b"\0" * 32,
    )


async def test_a_turn_answers_only_the_newest_message_and_only_the_users(chat: ChatEnv) -> None:
    """The job's message must be the conversation's newest and the user's own: an older message
    (already answered) or an answer of the assistant is not answered again, and nothing is sent."""
    who = await user(chat, "synthetic-chat-o@example.com")
    conv = await conversation(chat, who)
    await send(who, conv, "synthetic first question")
    await chat.run_next()
    rows = sql(
        "SELECT id, role FROM ai_messages WHERE owner_id = :o ORDER BY created_at, id", o=who.id
    ).all()
    assert [r.role for r in rows] == ["user", "assistant"]
    before = messages(who.id)
    attempts = chat.fake.attempts
    for named in (rows[0].id, rows[1].id):  # an older user message, then the newest answer
        await _crafted_turn(who.id, conv, named)
        execution = await chat.run_next()
        assert execution is not None
        assert execution.result == "failed"
        failed = (
            sql(
                "SELECT error_category FROM jobs WHERE owner_id = :o AND state = 'failed'", o=who.id
            )
            .scalars()
            .all()
        )
        assert failed
        assert all(c == "invalid_input" for c in failed)
    assert chat.fake.attempts == attempts
    assert messages(who.id) == before
