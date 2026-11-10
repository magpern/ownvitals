"""The chat assistant's job kind ``ai_chat_turn`` (ADR 0014 sections 3, 4, 5 and 8; PLAN section
7.4; #208). It runs in the worker only; web stores the user's message and queues the job
(``ai.chat_store``), and the browser follows the job's events (ids only) and then reads the
answer through the API.

One run, for the job's user only (``job.ctx``; every statement also names the owner):

1. **Read** the conversation's newest messages in a fenced transaction. The message the job
   names must be the newest and the user's; a conversation that is gone (deleted, or withdrawn)
   ends the job as ``invalid_input`` without a call.
2. **The turn** (``ai.turn.run_turn``): every lock, the budget and consent are checked by the
   client before each call; tools read as the job's user. A refused or failed call, or a turn
   that ends without an answer, fails the job with a closed category (``chat_model.failure_for``)
   and stores nothing; a cancel (a withdrawal, a deleted conversation) stops it.
3. **Store** the answer in the job's final fenced transaction (``Complete(store)``), which a
   cancel requested meanwhile replaces by ``cancelled``. The store first reads the live consent
   ``FOR SHARE NOWAIT`` (``consent.require_consent``): a withdrawal that holds the row, or has
   committed, means nothing is stored (``ai_not_enabled``); otherwise the withdrawal waits for
   this commit and then deletes the answer. Then the conversation row (``FOR UPDATE``; gone:
   ``invalid_input``), the answer and the conversation's ``updated_at``.

The model's text is stored as it is (plain text, rendered as text by the browser); an answer the
``ai_messages`` check would refuse is not cut short but refused (``ai_unavailable``).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text

from ownvitals.ai import consent
from ownvitals.ai.chat_model import (
    CHAT_KIND,
    HISTORY_READ,
    ChatInput,
    answer_fits,
    failure_for,
    history,
)
from ownvitals.ai.gates import ModelFailed, ModelRefused
from ownvitals.ai.turn import run_turn
from ownvitals.config import Settings
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.fence import FencedSession
from ownvitals.jobs.run import JobRun

_CANCELLED: Final = frozenset({"cancelled", "lease_lost"})

_NEWEST = text(
    "SELECT id, role, content FROM ai_messages WHERE owner_id = :o AND conversation_id = :c "
    "ORDER BY created_at DESC, id DESC LIMIT :limit"
)
_LOCK = text("SELECT id FROM ai_conversations WHERE owner_id = :o AND id = :c FOR UPDATE")
_ANSWER = text(
    "INSERT INTO ai_messages (owner_id, conversation_id, role, content) "
    "VALUES (:o, :c, 'assistant', :content)"
)
_TOUCH = text("UPDATE ai_conversations SET updated_at = now() WHERE owner_id = :o AND id = :c")


async def _history(job: JobRun, parsed: ChatInput) -> list[Any]:
    """The newest stored messages, oldest first; ``invalid_input`` unless the job's message is
    the newest and the user's."""
    params = {"o": job.ctx.user_id, "c": parsed.conversation_id, "limit": HISTORY_READ}
    async with job.fenced() as fs:
        rows = (await fs.db.execute(_NEWEST, params)).all()
    if not rows or rows[0].id != parsed.message_id or rows[0].role != "user":
        raise JobFailure("invalid_input")
    return list(reversed(rows))


async def _stopped_or_failed(job: JobRun, code: str) -> JobFailure:
    """A cancel requested meanwhile stops the run here (``RunStopped``); else the category."""
    if code in _CANCELLED:
        await job.checkpoint()
    return JobFailure(failure_for(code))


def _store(
    owner: UUID, conversation_id: UUID, answer: str
) -> Callable[[FencedSession], Awaitable[None]]:
    async def store(fs: FencedSession) -> None:
        try:
            await consent.require_consent(fs.db, owner, nowait=True)
        except consent.AiNotEnabled:
            raise JobFailure("ai_not_enabled") from None
        params = {"o": owner, "c": conversation_id}
        if (await fs.db.execute(_LOCK, params)).first() is None:
            raise JobFailure("invalid_input")
        await fs.db.execute(_ANSWER, {**params, "content": answer})
        await fs.db.execute(_TOUCH, params)

    return store


async def run_chat_turn(
    job: JobRun,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    **call_options: Any,
) -> Complete:
    """Answer the user's newest message of one conversation; ``call_options`` reach the model
    client (the tests' fake transport and prices)."""
    parsed = job.input
    if not isinstance(parsed, ChatInput):
        raise JobFailure("invalid_input")
    settings = settings if settings is not None else Settings()
    rows = await _history(job, parsed)
    try:
        turn = await run_turn(
            job,
            settings,
            kind="chat",
            history=history(rows),
            now=now or datetime.now(UTC),
            **call_options,
        )
    except ModelRefused as refused:
        raise await _stopped_or_failed(job, refused.code) from None
    except ModelFailed as failed:
        raise await _stopped_or_failed(job, failed.code) from None
    if turn.stopped != "answered" or turn.text is None or not answer_fits(turn.text):
        raise JobFailure("ai_unavailable")
    return Complete(_store(job.ctx.user_id, parsed.conversation_id, turn.text))


def register_chat_kind(registry: KindRegistry) -> JobKind:
    """``ai_chat_turn`` in the worker's registry (never the API's: web only queues it)."""
    return registry.register(JobKind(CHAT_KIND, ChatInput, run_chat_turn))
