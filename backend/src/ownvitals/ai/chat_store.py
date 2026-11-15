"""The web side of the chat assistant (ADR 0014 sections 3, 5 and 7; #208): the signed-in user's
own conversations, in their ``user_session`` (forced RLS; every statement also names the owner).
Web never calls the model: a message is stored and its ``ai_chat_turn`` job queued in one
transaction, and the worker answers (``ai.assistant``).

- ``post_message``: in this order, in one transaction: the flag and a live consent
  (``consent.require_enabled``, the consent row ``FOR SHARE``, so a withdrawal waits for this
  transaction and then cancels the job and deletes the message, or commits first and this one is
  refused), the owner's job-creation lock, the idempotency key (the same key and text again
  answer the first message and job), the conversation (``FOR UPDATE``; another user's is not
  found), no turn of it still running, the month's budget not used up, the per-user job limit;
  then the chat limit (``reserve``, a slot taken only when nothing else refuses), the message and
  the job.
- ``delete_conversation``: cancels the conversation's queued or running turn (job rows first,
  then the conversation, the order the worker's store takes them) and deletes it with its
  messages. No step-up: deleting one's own history stays easy.
- Reads: the list, one conversation with its latest turn (the job's state and category, ids
  only), and whether the month's budget is used up.
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai import budget, consent
from ownvitals.ai.chat_model import CHAT_KIND, LIST_LIMIT, MESSAGES_LIMIT, job_key
from ownvitals.config import Settings
from ownvitals.context import UserContext, require_context
from ownvitals.db import user_session
from ownvitals.jobs import user as user_jobs

Factory = async_sessionmaker[AsyncSession]
ACTIVE: Final = ("queued", "running")

_NEW = text(
    "INSERT INTO ai_conversations (id, owner_id) VALUES (:id, :o) RETURNING created_at, updated_at"
)
_LIST = text(
    "SELECT c.id, c.created_at, c.updated_at, "
    "(SELECT count(*) FROM ai_messages AS m WHERE m.owner_id = :o AND m.conversation_id = c.id) "
    "AS messages FROM ai_conversations AS c WHERE c.owner_id = :o "
    "ORDER BY c.updated_at DESC, c.id LIMIT :limit"
)
_ONE = text(
    "SELECT id, created_at, updated_at FROM ai_conversations WHERE owner_id = :o AND id = :c"
)
_LOCK = text("SELECT id FROM ai_conversations WHERE owner_id = :o AND id = :c FOR UPDATE")
_MESSAGES = text(
    "SELECT id, role, content, created_at FROM (SELECT id, role, content, created_at "
    "FROM ai_messages WHERE owner_id = :o AND conversation_id = :c "
    "ORDER BY created_at DESC, id DESC LIMIT :limit) AS newest ORDER BY created_at, id"
)
# The conversation's turns are its ``ai_chat_turn`` jobs (the input names it; ids only).
_TURNS = (
    "SELECT id, state, error_category FROM jobs WHERE owner_id = :o AND kind = :kind "
    "AND input->>'conversation_id' = :c"
)
_LATEST_TURN = text(_TURNS + " ORDER BY created_at DESC, id DESC LIMIT 1")
_ACTIVE_TURNS = text(_TURNS + " AND state IN ('queued', 'running') ORDER BY id")
_BY_KEY = text(
    "SELECT id, input, request_hash FROM jobs WHERE owner_id = :o AND idempotency_key = :key"
)
_INSERT_MESSAGE = text(
    "INSERT INTO ai_messages (id, owner_id, conversation_id, role, content) "
    "VALUES (:id, :o, :c, 'user', :content)"
)
_TOUCH = text("UPDATE ai_conversations SET updated_at = now() WHERE owner_id = :o AND id = :c")
_DELETE = text("DELETE FROM ai_conversations WHERE owner_id = :o AND id = :c RETURNING id")


class ChatRefused(Exception):
    """A request that is refused: ``reason`` is a response code of the ``ai`` family."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ConversationView:
    id: UUID
    created_at: datetime
    updated_at: datetime
    messages: int


@dataclass(frozen=True, slots=True)
class MessageView:
    id: UUID
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TurnView:
    """The conversation's latest turn: its job and how it ended (a closed category)."""

    job_id: UUID
    state: str
    failure: str | None


@dataclass(frozen=True, slots=True)
class ConversationDetail:
    conversation: ConversationView
    messages: list[MessageView]
    turn: TurnView | None


@dataclass(frozen=True, slots=True)
class Posted:
    message_id: UUID
    job_id: UUID
    created: bool  # False: the same request again (the first message and job)


@dataclass(frozen=True, slots=True)
class BudgetState:
    paused: bool
    resets_on: date


async def _enabled(db: AsyncSession, ctx: UserContext, settings: Settings) -> None:
    try:
        await consent.require_enabled(db, ctx.user_id, settings)
    except consent.AiNotEnabled:
        raise ChatRefused("ai_not_enabled") from None


async def create_conversation(
    factory: Factory,
    ctx: UserContext,
    settings: Settings,
    *,
    reserve: Callable[[], Awaitable[None]] | None = None,
) -> ConversationView:
    """A new, empty conversation; only while AI is enabled for the user. ``reserve`` (the
    creation limit) is taken last, so a refused request takes no slot."""
    require_context(ctx)
    new_id = uuid.uuid4()
    async with user_session(factory, ctx) as db:
        await _enabled(db, ctx, settings)
        if reserve is not None:
            await reserve()
        row = (await db.execute(_NEW, {"id": new_id, "o": ctx.user_id})).one()
    return ConversationView(new_id, row.created_at, row.updated_at, 0)


async def list_conversations(factory: Factory, ctx: UserContext) -> list[ConversationView]:
    """The user's conversations, the most recently used first (also while AI is off)."""
    require_context(ctx)
    async with user_session(factory, ctx) as db:
        rows = (await db.execute(_LIST, {"o": ctx.user_id, "limit": LIST_LIMIT})).all()
    return [ConversationView(r.id, r.created_at, r.updated_at, int(r.messages)) for r in rows]


def _turn(row: Any) -> TurnView | None:
    return None if row is None else TurnView(row.id, str(row.state), row.error_category)


async def read_conversation(
    factory: Factory, ctx: UserContext, conversation_id: UUID
) -> ConversationDetail | None:
    """One of the user's conversations, its newest messages in order and its latest turn."""
    require_context(ctx)
    params = {"o": ctx.user_id, "c": conversation_id}
    async with user_session(factory, ctx) as db:
        found = (await db.execute(_ONE, params)).first()
        if found is None:
            return None
        rows = (await db.execute(_MESSAGES, {**params, "limit": MESSAGES_LIMIT})).all()
        latest = (
            await db.execute(_LATEST_TURN, {**params, "c": str(conversation_id), "kind": CHAT_KIND})
        ).first()
    messages = [MessageView(r.id, str(r.role), str(r.content), r.created_at) for r in rows]
    view = ConversationView(found.id, found.created_at, found.updated_at, len(messages))
    return ConversationDetail(view, messages, _turn(latest))


async def delete_conversation(factory: Factory, ctx: UserContext, conversation_id: UUID) -> bool:
    """Cancel the conversation's running turn and delete it with its messages; ``False`` if the
    user has no such conversation."""
    require_context(ctx)
    params = {"o": ctx.user_id, "c": conversation_id}
    async with user_session(factory, ctx) as db:
        turns = (
            await db.execute(
                _ACTIVE_TURNS, {**params, "c": str(conversation_id), "kind": CHAT_KIND}
            )
        ).all()
        for turn in turns:  # the job rows first, as the worker's store locks them
            await user_jobs.cancel(db, ctx, turn.id)
        return (await db.execute(_DELETE, params)).first() is not None


def _request_hash(conversation_id: UUID, content: str) -> bytes:
    body = json.dumps({"conversation_id": str(conversation_id), "content": content})
    return hashlib.sha256(body.encode()).digest()


async def post_message(
    factory: Factory,
    ctx: UserContext,
    settings: Settings,
    conversation_id: UUID,
    content: str,
    request_id: str,
    *,
    job_limit: int,
    reserve: Callable[[], Awaitable[None]],
    now: datetime,
) -> Posted:
    """Store the user's message and queue its turn, or ``ChatRefused`` (nothing written)."""
    require_context(ctx)
    key = job_key(request_id)
    digest = _request_hash(conversation_id, content)
    params = {"o": ctx.user_id, "c": conversation_id}
    async with user_session(factory, ctx) as db:
        await _enabled(db, ctx, settings)  # the consent row before any job row
        await user_jobs.lock_owner(db, ctx)
        found = (await db.execute(_BY_KEY, {"o": ctx.user_id, "key": key})).first()
        if found is not None:
            if bytes(found.request_hash) != digest:
                raise ChatRefused("idempotency_mismatch")
            return Posted(UUID(found.input["message_id"]), found.id, created=False)
        if (await db.execute(_LOCK, params)).first() is None:
            raise ChatRefused("not_found")
        turn_params = {**params, "c": str(conversation_id), "kind": CHAT_KIND}
        if (await db.execute(_ACTIVE_TURNS, turn_params)).first() is not None:
            raise ChatRefused("ai_turn_in_progress")
        if (await budget.month_budget(db, ctx, settings, now)).paused:
            raise ChatRefused("ai_budget_exhausted")
        if await user_jobs.active_jobs(db, ctx) >= job_limit:
            raise ChatRefused("busy")
        await reserve()
        message_id = uuid.uuid4()
        await db.execute(_INSERT_MESSAGE, {**params, "id": message_id, "content": content})
        await db.execute(_TOUCH, params)
        job_input = {"conversation_id": str(conversation_id), "message_id": str(message_id)}
        new = user_jobs.NewJob(
            kind=CHAT_KIND,
            input=json.dumps(job_input, sort_keys=True, separators=(",", ":")),
            idempotency_key=key,
            request_hash=digest,
        )
        created = await user_jobs.create(db, ctx, new, limit=job_limit)
        if created.job_id is None:  # the owner lock makes this unreachable
            raise ChatRefused("busy")
    return Posted(message_id, created.job_id, created=True)


async def budget_state(
    factory: Factory, ctx: UserContext, settings: Settings, now: datetime
) -> BudgetState:
    """Whether the user's monthly budget is used up, and when it resets (no amounts)."""
    async with user_session(factory, ctx) as db:
        month = await budget.month_budget(db, ctx, settings, now)
    return BudgetState(month.paused, month.resets_on)
