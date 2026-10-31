"""A tiny in-process stand-in for the webhook receiver of the app under test (an ASGI app).

It answers the HEAD check of ``notify subscribe`` and the POST of a webhook, records what it got,
and can misbehave on purpose: an answer status of its own, a redirect, a handler that takes long
(on the fake clock, or in real time). Synthetic only; nothing touches the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping
from typing import Any
from urllib.parse import parse_qsl

import httpx

from withingscore import FakeWithings
from withingswire import ManualClock

HOST = "https://app.example.test"


class Receiver:
    def __init__(self, fake: FakeWithings | None = None) -> None:
        self.fake = fake
        self.heads: list[str] = []  # paths HEADed
        self.subscriptions_at_head: list[int] = []  # subscriptions the fake had at each HEAD
        self.posts: list[dict[str, str]] = []
        self.post_paths: list[str] = []
        self.head_status = 200
        self.post_status = 200
        self.redirect_to: str | None = None  # a Location header on the POST answer
        self.clock_seconds = 0.0  # the handler takes this long on the fake clock
        self.sleep_seconds = 0.0  # ... and this long in real time

    def client(self, *, follow_redirects: bool = False) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self),
            base_url=HOST,
            follow_redirects=follow_redirects,
        )

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers: list[tuple[bytes, bytes]] = []
        if scope["method"] == "HEAD":
            self.heads.append(scope["path"])
            self.subscriptions_at_head.append(len(self.fake.subscriptions) if self.fake else 0)
            status = self.head_status
        else:
            self.post_paths.append(scope["path"])
            self.posts.append(dict(parse_qsl(body.decode())))
            if self.clock_seconds and self.fake is not None:
                assert isinstance(self.fake.clock, ManualClock)
                self.fake.clock.advance(self.clock_seconds)
            if self.sleep_seconds:
                await asyncio.sleep(self.sleep_seconds)
            status = self.post_status
            if self.redirect_to is not None:
                headers.append((b"location", self.redirect_to.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})
