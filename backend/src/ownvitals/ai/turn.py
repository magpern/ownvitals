"""One chat or digest turn: the tool loop around the model client (ADR 0014 sections 4, 5 and 8;
#207).

``run_turn(job, settings, kind=..., history=..., now=...)`` sends the conversation's last
messages (``requests.history_window``) through ``client.call_model``; while the answer asks for
tools, it runs each one with ``ai.tools.run_tool`` **as the job's user** (their ``UserContext``,
forced RLS, read-only, closed arguments) and sends the results back, at most
``MAX_TOOL_CALLS_PER_TURN`` tool calls in all. The only things that reach a request are the
user's own messages, the tools' allowlisted results and our fixed prompt and definitions.

The model's word authorises nothing: a tool request is only a read for the same user, an unknown
tool ends the turn without an answer (``unknown_tool``), and too many tool calls end it too
(``tool_limit``). The answer is text the caller stores in the user's own conversation, after
its own consent check (``consent.require_consent`` in the storing transaction).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ownvitals.ai.client import call_model
from ownvitals.ai.gates import ModelRefused
from ownvitals.ai.requests import Kind, Message, Text, ToolOutput, ToolUse, history_window
from ownvitals.ai.tools import run_tool
from ownvitals.ai.tools_model import MAX_TOOL_CALLS_PER_TURN, TOOLS
from ownvitals.config import Settings
from ownvitals.jobs import JobRun

Stopped = Literal["answered", "no_answer", "tool_limit", "unknown_tool"]


@dataclass(frozen=True, slots=True)
class Turn:
    """How a turn ended: the answer's text (only when ``answered``) and counts."""

    text: str | None
    stopped: Stopped
    tool_calls: int
    requests: int


async def run_turn(
    job: JobRun,
    settings: Settings,
    *,
    kind: Kind,
    history: list[Message],
    now: datetime,
    **call_options: Any,
) -> Turn:
    """Run one turn for ``job``'s user; ``history`` ends with the user's new message."""
    messages = history_window(history)
    if not messages or messages[-1].role != "user":
        raise ModelRefused("invalid_messages")
    calls = requests = 0
    while True:
        reply = await call_model(job, settings, kind=kind, messages=messages, **call_options)
        requests += 1
        if not reply.tool_requests:
            text = reply.text
            return Turn(text or None, "answered" if text else "no_answer", calls, requests)
        if any(r.name not in TOOLS for r in reply.tool_requests):
            return Turn(None, "unknown_tool", calls, requests)
        if calls + len(reply.tool_requests) > MAX_TOOL_CALLS_PER_TURN:
            return Turn(None, "tool_limit", calls, requests)
        uses: list[ToolUse] = []
        outputs: list[ToolOutput] = []
        for request in reply.tool_requests:
            calls += 1
            result = await run_tool(
                job.factory, job.ctx, settings, request.name, request.arguments, now=now
            )
            uses.append(ToolUse(calls, request.name, _mapping(request.arguments)))
            outputs.append(ToolOutput(calls, result))
        said = tuple(Text(t) for t in reply.texts)
        messages = [
            *messages,
            Message("assistant", (*said, *uses)),
            Message("user", tuple(outputs)),
        ]


def _mapping(arguments: object) -> dict[str, Any]:
    """The raw arguments as a dict (``requests`` keeps only their validated form)."""
    return dict(arguments) if isinstance(arguments, dict) else {}  # pyright: ignore[reportUnknownArgumentType]
