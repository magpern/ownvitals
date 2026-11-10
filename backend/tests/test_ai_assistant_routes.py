"""The chat assistant's API (ADR 0014 sections 3, 5, 7 and 8; #208) on a real database as the app
role under forced RLS: a user cannot read, continue or delete another user's conversation; a
message is refused without the flag or consent, at the cap, while a turn runs, above the chat
limit and with a reused request id; a delete stops the running turn.

Synthetic users and text; no model call (nothing here runs a turn but where it says so).
"""

import ast
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from aichat import (  # noqa: F401 - fixtures
    ChatEnv,
    _ai_worker,
    chat_fixture,
    chat_secrets_fixture,
    conversation,
    count,
    request_id,
    send,
    sql,
    user,
)
from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from fakemodel import text as answer
from flows import reason
from ownvitals.ai.chat_model import CHAT_KIND
from ownvitals.response_codes import AI
from test_response_codes import assert_response_code

SYNTHETIC = True
SRC = Path(__file__).resolve().parent.parent / "src" / "ownvitals"


@contextmanager
def settings_changed(chat: ChatEnv, **values: Any) -> Iterator[None]:
    """The running app's settings with ``values`` for the duration (routes read them per call)."""
    state = chat.env.app.state
    before = state.settings
    state.settings = before.model_copy(update=values)
    try:
        yield
    finally:
        state.settings = before


def _refused(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert_response_code("ai", response.json())
    assert reason(response) == code


def _finish_turns(owner: uuid.UUID) -> None:
    sql(
        "UPDATE jobs SET state = 'completed', finished_at = now() WHERE owner_id = :o "
        "AND state IN ('queued', 'running')",
        o=owner,
    )


# --- RLS: another user's conversation ------------------------------------------------------------


async def test_another_users_conversation_cannot_be_read_continued_or_deleted(
    chat: ChatEnv,
) -> None:
    alice = await user(chat, "synthetic-route-a@example.com")
    bob = await user(chat, "synthetic-route-b@example.com")
    conv = await conversation(chat, alice)
    assert (await send(alice, conv, "synthetic question of alice")).status_code == 202
    _finish_turns(alice.id)

    _refused(await bob.browser.get(f"/api/ai/conversations/{conv}"), 404, "not_found")
    _refused(await send(bob, conv, "synthetic message from bob"), 404, "not_found")
    _refused(await bob.browser.request("DELETE", f"/api/ai/conversations/{conv}"), 404, "not_found")
    listed = (await bob.browser.get("/api/ai/conversations")).json()["conversations"]
    assert listed == []
    # Alice's conversation is untouched and Bob got no job.
    assert count("ai_messages", alice.id) == 1
    assert count("ai_conversations", alice.id) == 1
    jobs = sql("SELECT count(*) FROM jobs WHERE owner_id = :o", o=bob.id).scalar_one()
    assert jobs == 0
    # The same id is the same answer as an unknown one, and a malformed id too.
    _refused(await bob.browser.get(f"/api/ai/conversations/{uuid.uuid4()}"), 404, "not_found")
    _refused(await bob.browser.get("/api/ai/conversations/not-a-uuid"), 404, "not_found")


async def test_the_list_holds_only_the_callers_conversations_newest_first(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-c@example.com")
    bob = await user(chat, "synthetic-route-d@example.com")
    first = await conversation(chat, alice)
    second = await conversation(chat, alice)
    await conversation(chat, bob)
    await send(alice, first, "synthetic question")
    listed = (await alice.browser.get("/api/ai/conversations")).json()["conversations"]
    assert [c["id"] for c in listed] == [str(first), str(second)]
    assert [c["messages"] for c in listed] == [1, 0]


async def test_the_routes_need_a_signed_in_user(chat: ChatEnv) -> None:
    for path in ("/api/ai/conversations", "/api/ai/budget"):
        assert (await chat.env.client.get(path)).status_code == 401


# --- refusals ---------------------------------------------------------------------------------


async def test_without_consent_or_the_flag_nothing_is_created_or_queued(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-e@example.com", consent=False)
    _refused(await alice.browser.post("/api/ai/conversations"), 409, "ai_not_enabled")
    bob = await user(chat, "synthetic-route-f@example.com")
    conv = await conversation(chat, bob)
    with settings_changed(chat, ai_insights=False):
        _refused(await send(bob, conv, "synthetic question"), 409, "ai_not_enabled")
        # Reading and deleting stay possible while AI is off.
        assert (await bob.browser.get(f"/api/ai/conversations/{conv}")).status_code == 200
    sql("UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o", o=bob.id)
    _refused(await send(bob, conv, "synthetic question"), 409, "ai_not_enabled")
    assert count("ai_messages", bob.id) == 0
    assert sql("SELECT count(*) FROM jobs WHERE owner_id = :o", o=bob.id).scalar_one() == 0


async def test_at_the_cap_the_message_is_refused_and_the_budget_says_so(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-g@example.com")
    conv = await conversation(chat, alice)
    budget = (await alice.browser.get("/api/ai/budget")).json()
    assert budget["paused"] is False
    with settings_changed(chat, ai_user_monthly_cap_microusd=0, ai_global_monthly_cap_microusd=0):
        _refused(await send(alice, conv, "synthetic question"), 409, "ai_budget_exhausted")
        budget = (await alice.browser.get("/api/ai/budget")).json()
    assert budget["paused"] is True
    assert set(budget) == {"paused", "resets_on"}  # no amounts
    assert budget["resets_on"].endswith("-01")
    assert count("ai_messages", alice.id) == 0


async def test_one_turn_at_a_time_per_conversation(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-h@example.com")
    conv = await conversation(chat, alice)
    assert (await send(alice, conv, "synthetic first")).status_code == 202
    _refused(await send(alice, conv, "synthetic second"), 409, "ai_turn_in_progress")
    assert count("ai_messages", alice.id) == 1


async def test_a_repeated_request_id_answers_the_first_message(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-i@example.com")
    conv = await conversation(chat, alice)
    key = request_id()
    first = await send(alice, conv, "synthetic question", request_id=key)
    again = await send(alice, conv, "synthetic question", request_id=key)
    assert first.status_code == again.status_code == 202
    assert first.json() == again.json()
    _refused(
        await send(alice, conv, "synthetic other", request_id=key), 409, "idempotency_mismatch"
    )
    assert count("ai_messages", alice.id) == 1


async def test_malformed_messages_are_refused(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-j@example.com")
    conv = await conversation(chat, alice)
    _refused(await send(alice, conv, "   \n  "), 422, "invalid_input")
    for body in (
        {"content": "x" * 16_001, "request_id": request_id()},
        {"content": "synthetic", "request_id": "short"},
        {"content": "synthetic", "request_id": request_id(), "owner_id": str(alice.id)},
    ):
        response = await alice.browser.post(f"/api/ai/conversations/{conv}/messages", body)
        assert response.status_code == 422, body
    assert count("ai_messages", alice.id) == 0


async def test_the_chat_limit_is_twenty_messages_an_hour(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-k@example.com")
    conv = await conversation(chat, alice)
    for n in range(20):
        assert (await send(alice, conv, f"synthetic {n}")).status_code == 202, n
        _finish_turns(alice.id)
    over = await send(alice, conv, "synthetic 21")
    _refused(over, 429, "rate_limited")
    assert over.headers.get("retry-after")
    assert count("ai_messages", alice.id) == 20


async def test_a_refusal_takes_no_slot_of_the_chat_limit(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-l@example.com")
    conv = await conversation(chat, alice)
    await send(alice, conv, "synthetic first")
    for _ in range(25):  # each refused while the turn runs: no slot taken
        _refused(await send(alice, conv, "synthetic again"), 409, "ai_turn_in_progress")
    _finish_turns(alice.id)
    assert (await send(alice, conv, "synthetic second")).status_code == 202


# --- delete --------------------------------------------------------------------------------------


async def test_delete_stops_the_running_turn_and_removes_the_conversation(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-m@example.com")
    conv = await conversation(chat, alice)
    keep = await conversation(chat, alice)
    job_id = (await send(alice, conv, "synthetic question")).json()["job_id"]
    await send(alice, keep, "synthetic kept question")
    deleted = await alice.browser.request("DELETE", f"/api/ai/conversations/{conv}")
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    state = sql("SELECT state FROM jobs WHERE id = :j", j=uuid.UUID(job_id)).scalar_one()
    assert state == "cancelled"
    assert count("ai_conversations", alice.id) == 1
    assert count("ai_messages", alice.id) == 1
    # The other conversation's turn still runs and is answered.
    chat.fake.script = [answer("Synthetic kept answer.")]
    execution = await chat.run_next()
    assert execution is not None
    assert execution.result == "completed"
    assert count("ai_messages", alice.id) == 2


async def test_a_turn_whose_conversation_was_deleted_stores_nothing(chat: ChatEnv) -> None:
    alice = await user(chat, "synthetic-route-n@example.com")
    conv = await conversation(chat, alice)
    await send(alice, conv, "synthetic question")
    sql("DELETE FROM ai_conversations WHERE id = :c", c=conv)  # as a delete racing the claim
    execution = await chat.run_next()
    assert execution is not None
    assert execution.result == "failed"
    assert chat.fake.attempts == 0
    assert count("ai_messages", alice.id) == 0


# --- codes ---------------------------------------------------------------------------------------


def test_every_code_the_store_refuses_with_is_an_ai_code() -> None:
    tree = ast.parse((SRC / "ai" / "chat_store.py").read_text())
    codes = {
        str(node.args[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "ChatRefused"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert codes
    assert codes <= AI, sorted(codes - AI)


def test_the_job_kind_is_the_consent_modules_ai_kind() -> None:
    from ownvitals.ai.consent import AI_JOB_KINDS

    assert CHAT_KIND in AI_JOB_KINDS  # a withdrawal cancels it
