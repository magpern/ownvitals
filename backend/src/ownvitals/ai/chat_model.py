"""The chat assistant's rules, without I/O (ADR 0014 sections 5, 7 and 8; PLAN section 7.4; #208).

- ``CHAT_KIND`` and ``ChatInput``: the job a message queues (ids only: the text stays in
  ``ai_messages``, never on a job row or an event).
- ``failure_for``: the job failure category of a turn that ended without an answer, from the
  model client's closed codes (``ai.gates.ModelRefused`` / ``ModelFailed``) or the turn's end.
  Three ``ai_*`` categories (migration ``0024``), none retried: each try may be billed.
- ``history``: the stored rows as ``ai.requests.Message`` text messages, oldest first.
- ``REQUEST_ID``: the browser's idempotency key for one message.
"""

import re
from collections.abc import Iterable
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from ownvitals.ai.requests import Message, Text
from ownvitals.ai.tables import MESSAGE_MAX_CHARS

CHAT_KIND: Final = "ai_chat_turn"
# The same pattern as the jobs API's ``Idempotency-Key`` (the ``jobs.idempotency_key`` check),
# less the prefix ``chat-`` the job key adds.
REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9_-]{16,59}$")
JOB_KEY_PREFIX: Final = "chat-"
# How many stored messages a turn reads: more than ``requests.MAX_HISTORY``, which then cuts the
# window so it starts with a user message.
HISTORY_READ: Final = 40
LIST_LIMIT: Final = 50  # conversations in the list, newest first
MESSAGES_LIMIT: Final = 200  # messages shown of one conversation, the newest

Failure = Literal["ai_not_enabled", "ai_budget_exhausted", "ai_unavailable", "outbound_blocked"]

# The client's codes that mean the user's consent or the flag is not (or no longer) there.
_NOT_ENABLED: Final = frozenset({"ai_not_enabled", "consent"})


class ChatInput(BaseModel):
    """The ``ai_chat_turn`` job's input: the conversation and the user's message to answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID
    message_id: UUID


def failure_for(code: str) -> Failure:
    """The job category for a refused or failed model call (``code`` is closed)."""
    if code in _NOT_ENABLED:
        return "ai_not_enabled"
    if code == "ai_budget_exhausted":
        return "ai_budget_exhausted"
    if code == "outbound_blocked":
        return "outbound_blocked"
    return "ai_unavailable"


def job_key(request_id: str) -> str:
    """The job's idempotency key for the browser's ``request_id`` (``REQUEST_ID`` checked)."""
    if not REQUEST_ID.fullmatch(request_id):
        raise ValueError("a request id is 16 to 59 of A-Z, a-z, 0-9, _ and -")
    return JOB_KEY_PREFIX + request_id


def valid_text(content: str) -> bool:
    """A message the user may send: 1 to ``MESSAGE_MAX_CHARS`` characters, not only blanks."""
    return 0 < len(content) <= MESSAGE_MAX_CHARS and content.strip() != ""


def history(rows: Iterable[Any]) -> list[Message]:
    """Stored rows (``role``, ``content``; oldest first) as text messages for a request."""
    return [Message(row.role, (Text(row.content),)) for row in rows]


def answer_fits(text: str) -> bool:
    """An answer that the ``ai_messages.content`` check admits as it is (never cut short)."""
    return valid_text(text)
