"""``/api/ai/conversations`` and ``/api/ai/budget``: the chat assistant's API (ADR 0014 sections
3, 5, 7 and 8; PLAN section 7.4; #208).

- ``GET /conversations``: the user's conversations (also while AI is off, so they can be read
  and deleted). ``POST /conversations``: a new one, only while AI is enabled for the user.
- ``GET /conversations/{id}``: its newest messages in order and its latest turn (the job id, its
  state and a closed failure category); the browser follows a running turn on
  ``/api/jobs/{job_id}/events`` (ids only, T47) and then reads the answer here.
- ``POST /conversations/{id}/messages``: ``content`` (1 to 16,000 characters) and
  ``request_id`` (the browser's idempotency key). 202 with the message and job ids. Refused:
  409 ``ai_not_enabled``, ``ai_budget_exhausted``, ``ai_turn_in_progress``,
  ``idempotency_mismatch``; 429 ``busy`` (the job limit) or ``rate_limited`` (20 messages per
  hour, ``Retry-After``); 404 for a conversation that is not the user's.
- ``DELETE /conversations/{id}``: stops its running turn and deletes it with its messages.
- ``GET /budget``: whether this month's budget is used up and the reset date (no amounts).

A full session for every route (``require_user``); every write goes through the auth
middleware's Origin and CSRF checks. Web never calls the model. Responses carry codes, ids,
dates and the user's own stored text, never a sentence of ours.
"""

from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ownvitals.ai import chat_store
from ownvitals.ai.chat_model import REQUEST_ID, valid_text
from ownvitals.ai.chat_store import ChatRefused, ConversationView
from ownvitals.ai.tables import MESSAGE_MAX_CHARS
from ownvitals.auth.deps import require_user, reserve_ai_chat
from ownvitals.context import UserContext

router = APIRouter(prefix="/api/ai")

_STATUS = {"not_found": 404, "busy": 429, "invalid_input": 422}


class ConversationOut(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    messages: int


class ConversationList(BaseModel):
    conversations: list[ConversationOut]


class ConversationOne(BaseModel):
    conversation: ConversationOut


class MessageOut(BaseModel):
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class TurnOut(BaseModel):
    job_id: UUID
    state: str
    failure: str | None


class ConversationDetailOut(BaseModel):
    conversation: ConversationOut
    messages: list[MessageOut]
    turn: TurnOut | None


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS)
    request_id: str = Field(pattern=REQUEST_ID.pattern)


class PostedOut(BaseModel):
    message_id: UUID
    job_id: UUID


class DeletedOut(BaseModel):
    deleted: bool


class BudgetOut(BaseModel):
    paused: bool
    resets_on: date


def _refuse(reason: str) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(reason, 409), detail={"reason": reason})


def _id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise _refuse("not_found") from None


def _out(view: ConversationView) -> ConversationOut:
    return ConversationOut(
        id=view.id, created_at=view.created_at, updated_at=view.updated_at, messages=view.messages
    )


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ConversationList:
    found = await chat_store.list_conversations(request.app.state.session_factory, ctx)
    return ConversationList(conversations=[_out(v) for v in found])


@router.post("/conversations", status_code=201, response_model=ConversationOne)
async def create_conversation(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ConversationOne:
    state = request.app.state
    try:
        view = await chat_store.create_conversation(state.session_factory, ctx, state.settings)
    except ChatRefused as refused:
        raise _refuse(refused.reason) from None
    return ConversationOne(conversation=_out(view))


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailOut)
async def read_conversation(
    conversation_id: str, request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> ConversationDetailOut:
    found = await chat_store.read_conversation(
        request.app.state.session_factory, ctx, _id(conversation_id)
    )
    if found is None:
        raise _refuse("not_found")
    turn = found.turn
    return ConversationDetailOut(
        conversation=_out(found.conversation),
        messages=[
            MessageOut.model_validate(
                {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at}
            )
            for m in found.messages
        ],
        turn=None
        if turn is None
        else TurnOut(job_id=turn.job_id, state=turn.state, failure=turn.failure),
    )


@router.delete("/conversations/{conversation_id}", response_model=DeletedOut)
async def delete_conversation(
    conversation_id: str, request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> DeletedOut:
    done = await chat_store.delete_conversation(
        request.app.state.session_factory, ctx, _id(conversation_id)
    )
    if not done:
        raise _refuse("not_found")
    return DeletedOut(deleted=True)


@router.post("/conversations/{conversation_id}/messages", status_code=202, response_model=PostedOut)
async def post_message(
    conversation_id: str,
    body: MessageIn,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
) -> PostedOut:
    uid = _id(conversation_id)
    if not valid_text(body.content):
        raise _refuse("invalid_input")
    state = request.app.state

    async def reserve() -> None:
        await reserve_ai_chat(request, ctx)

    try:
        posted = await chat_store.post_message(
            state.session_factory,
            ctx,
            state.settings,
            uid,
            body.content,
            body.request_id,
            job_limit=state.settings.jobs_per_user,
            reserve=reserve,
            now=datetime.now(UTC),
        )
    except ChatRefused as refused:
        raise _refuse(refused.reason) from None
    return PostedOut(message_id=posted.message_id, job_id=posted.job_id)


@router.get("/budget", response_model=BudgetOut)
async def read_budget(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> BudgetOut:
    state = request.app.state
    found = await chat_store.budget_state(
        state.session_factory, ctx, state.settings, datetime.now(UTC)
    )
    return BudgetOut(paused=found.paused, resets_on=found.resets_on)
