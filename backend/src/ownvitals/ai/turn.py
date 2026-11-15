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

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ownvitals.ai.client import call_model
from ownvitals.ai.gates import ModelRefused
from ownvitals.ai.requests import (
    Kind,
    Message,
    Text,
    ToolOutput,
    ToolUse,
    build_request,
    history_window,
    serialize,
)
from ownvitals.ai.responses import Usage
from ownvitals.ai.tools import run_tool
from ownvitals.ai.tools_model import MAX_TOOL_CALLS_PER_TURN, TOOLS
from ownvitals.config import Settings
from ownvitals.jobs import JobRun

Stopped = Literal["answered", "no_answer", "tool_limit", "unknown_tool"]


@dataclass(frozen=True, slots=True)
class Turn:
    """How a turn ended: the answer's text (only when ``answered``) and counts.

    ``results`` are the turn's tool results in call order and ``sizes`` each request's byte
    length with the input tokens the provider reported for it (input, cache write and cache
    read). The production callers store neither; the owner's evaluation run grades numbers
    against ``results`` and reports the largest actual-to-bounded input ratio from ``sizes``
    (ADR 0014 section 9)."""

    text: str | None
    stopped: Stopped
    tool_calls: int
    requests: int
    results: tuple[Mapping[str, Any], ...] = ()
    sizes: tuple[tuple[int, int], ...] = ()


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
    results: list[Mapping[str, Any]] = []
    sizes: list[tuple[int, int]] = []

    def ended(text: str | None, stopped: Stopped) -> Turn:
        return Turn(text, stopped, calls, requests, tuple(results), tuple(sizes))

    while True:
        reply = await call_model(job, settings, kind=kind, messages=messages, **call_options)
        requests += 1
        sizes.append((_request_bytes(settings, kind, messages), _input_tokens(reply.usage)))
        if not reply.tool_requests:
            text = reply.text
            return ended(text or None, "answered" if text else "no_answer")
        if any(r.name not in TOOLS for r in reply.tool_requests):
            return ended(None, "unknown_tool")
        if calls + len(reply.tool_requests) > MAX_TOOL_CALLS_PER_TURN:
            return ended(None, "tool_limit")
        uses: list[ToolUse] = []
        outputs: list[ToolOutput] = []
        for request in reply.tool_requests:
            calls += 1
            result = await run_tool(
                job.factory, job.ctx, settings, request.name, request.arguments, now=now
            )
            uses.append(ToolUse(calls, request.name, _mapping(request.arguments)))
            outputs.append(ToolOutput(calls, result))
            results.append(result)
        said = tuple(Text(t) for t in reply.texts)
        messages = [
            *messages,
            Message("assistant", (*said, *uses)),
            Message("user", tuple(outputs)),
        ]


def _request_bytes(settings: Settings, kind: Kind, messages: list[Message]) -> int:
    """The byte length of the request just sent (the client built the same bytes)."""
    return len(serialize(build_request(settings.ai_model, kind, messages)))


def _input_tokens(usage: Usage) -> int:
    return usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens


def _mapping(arguments: object) -> dict[str, Any]:
    """The raw arguments as a dict (``requests`` keeps only their validated form)."""
    return dict(arguments) if isinstance(arguments, dict) else {}  # pyright: ignore[reportUnknownArgumentType]
