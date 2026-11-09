"""Reading a model answer (ADR 0014 sections 5 and 8; #207). Pure: no I/O.

The answer is untrusted. Only two kinds of content block are kept: ``text`` (bounded, kept as
text; the UI renders it as plain text) and ``tool_use`` (a request to run a read-only tool,
which the turn loop validates against the tool's closed arguments before anything runs). Every
other block is dropped. The usage must be four non-negative integers, or the answer is
``BadResponse`` and the call is charged at its reserved amount. The model's output is a
proposal: nothing here, and nothing that reads a ``Reply``, acts on it beyond running a
read-only tool for the same user and storing the text in that user's conversation.
"""

import json
from dataclasses import dataclass
from typing import Any, Final

from ownvitals.ai.requests import TEXT_MAX_CHARS

RESPONSE_MAX_BYTES: Final = 1024 * 1024
TOOL_USES_MAX: Final = 16  # more in one answer is malformed (the turn allows 8 in all)
_STOP_REASONS: Final = frozenset(
    {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"}
)


class BadResponse(ValueError):
    """A 200 answer that is not a well-formed Messages API reply."""


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """The model asked for ``name`` with ``arguments`` (raw: validated before any use)."""

    name: str
    arguments: object


@dataclass(frozen=True, slots=True)
class Reply:
    texts: tuple[str, ...]
    tool_requests: tuple[ToolRequest, ...]
    stop_reason: str
    usage: Usage

    @property
    def text(self) -> str:
        return "\n\n".join(self.texts)


def _count(usage: dict[str, Any], key: str, required: bool) -> int:
    value = usage.get(key)
    if value is None and not required:
        return 0
    if type(value) is not int or value < 0:
        raise BadResponse("usage")
    return value


def usage_of(payload: object) -> Usage:
    """The usage block of a reply."""
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):  # pyright: ignore[reportUnknownMemberType]
        raise BadResponse("usage")
    usage: dict[str, Any] = payload["usage"]  # pyright: ignore[reportUnknownVariableType]
    return Usage(
        input_tokens=_count(usage, "input_tokens", True),
        output_tokens=_count(usage, "output_tokens", True),
        cache_write_tokens=_count(usage, "cache_creation_input_tokens", False),
        cache_read_tokens=_count(usage, "cache_read_input_tokens", False),
    )


def parse_reply(raw: bytes) -> Reply:
    """A 200 answer's body; ``BadResponse`` for anything malformed or too large."""
    if len(raw) > RESPONSE_MAX_BYTES:
        raise BadResponse("too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise BadResponse("json") from None
    if not isinstance(payload, dict) or payload.get("type") != "message":  # pyright: ignore[reportUnknownMemberType]
        raise BadResponse("type")
    usage = usage_of(payload)
    stop = payload.get("stop_reason")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if stop not in _STOP_REASONS:
        raise BadResponse("stop_reason")
    content = payload.get("content")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if not isinstance(content, list):
        raise BadResponse("content")
    texts: list[str] = []
    tools: list[ToolRequest] = []
    for block in content:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(block, dict):
            raise BadResponse("block")
        kind = block.get("type")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if kind == "text":
            text = block.get("text")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if not isinstance(text, str) or len(text) > TEXT_MAX_CHARS:
                raise BadResponse("text")
            if text.strip():
                texts.append(text.replace("\x00", ""))
        elif kind == "tool_use":
            name = block.get("name")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if not isinstance(name, str) or len(tools) >= TOOL_USES_MAX:
                raise BadResponse("tool_use")
            tools.append(ToolRequest(name=name, arguments=block.get("input")))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return Reply(tuple(texts), tuple(tools), str(stop), usage)  # pyright: ignore[reportUnknownArgumentType]
