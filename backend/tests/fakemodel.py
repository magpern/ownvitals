"""A fake of the model provider's Messages API for every AI test (ADR 0014 sections 5 and 9;
#207). No network: it is the ``inner`` transport of ``egress.model_async_client``, so every
egress check (the worker role, the model host, the total deadline) still runs in front of it.

``FakeModel`` records every request it receives (method, URL, headers, the exact body bytes) and
answers from a script, one step per request: a text answer, a tool request, an error status, a
connection failure (never sent) or a timeout. With the script exhausted it answers a short text.
``before`` is an optional hook awaited before each answer (a pause, a withdrawal mid-call).

Everything it says is synthetic: no real model output, no real account, no health data.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

SYNTHETIC = True
MODEL = "synthetic-model-1"
API_KEY = "synthetic-anthropic-key-" + "K" * 24


@dataclass(frozen=True)
class Seen:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


def usage(input_tokens: int = 100, output_tokens: int = 20) -> dict[str, int]:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def message(content: list[dict[str, Any]], stop: str, **counts: int) -> dict[str, Any]:
    return {
        "id": "msg_synthetic",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop,
        "usage": usage(**counts),
    }


def text(answer: str = "Synthetic answer.", **counts: int) -> tuple[int, dict[str, Any]]:
    return 200, message([{"type": "text", "text": answer}], "end_turn", **counts)


def tools(*calls: tuple[str, dict[str, Any]], **counts: int) -> tuple[int, dict[str, Any]]:
    content = [
        {"type": "tool_use", "id": f"toolu_synthetic_{n}", "name": name, "input": args}
        for n, (name, args) in enumerate(calls)
    ]
    return 200, message(content, "tool_use", **counts)


def status(code: int, retry_after: str | None = None) -> tuple[int, dict[str, Any], dict[str, str]]:
    headers = {} if retry_after is None else {"retry-after": retry_after}
    return code, {"type": "error", "error": {"type": "synthetic"}}, headers


CONNECT = "connect"  # a connection failure before anything was sent
TIMEOUT = "timeout"  # a read timeout after the request was sent

Step = Any


@dataclass
class FakeModel:
    script: list[Step] = field(default_factory=list[Step])
    seen: list[Seen] = field(default_factory=list[Seen])
    attempts: int = 0  # every request the client tried, sent or not
    before: Callable[[Seen], Awaitable[None]] | None = None

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        seen = Seen(request.method, str(request.url), dict(request.headers), request.content)
        self.seen.append(seen)
        if self.before is not None:
            await self.before(seen)
        step = self.script.pop(0) if self.script else text()
        if step == CONNECT:
            self.seen.pop()  # never sent: the provider saw nothing
            raise httpx.ConnectError("synthetic connect failure", request=request)
        if step == TIMEOUT:
            raise httpx.ReadTimeout("synthetic read timeout", request=request)
        code, body, *rest = step
        return httpx.Response(code, json=body, headers=rest[0] if rest else None)

    def bodies(self) -> list[bytes]:
        return [s.body for s in self.seen]
