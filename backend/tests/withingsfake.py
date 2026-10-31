"""The Withings fake as an httpx transport, for the backend tests.

``FakeWithingsTransport`` plugs ``withingscore.FakeWithings`` into an ``httpx.AsyncClient``;
the code under test makes its usual HTTPS calls to ``wbsapi.withings.net`` and
``account.withings.com`` and nothing leaves the process (no socket is opened, so the network
guard has nothing to block). Synthetic data only; see ``withingsdata``.

    fake, app, user = make_fake()                     # one app, one synthetic user
    tokens = fake.tokens_for(app.client_id, user.userid)
    async with fake_client(fake) as client:           # httpx.AsyncClient over the fake
        reply = await client.post(
            "https://wbsapi.withings.net/measure",
            data={"action": "getmeas"},
            headers={"authorization": f"Bearer {tokens['access_token']}"},
        )

Failures: ``fake.inject("drop", at=2, endpoint="v2/oauth2:requesttoken", when="after")`` loses
the response of the second refresh after the server rotated the token (the client sees
``httpx.ReadError``); ``"delay"`` holds a response back (the manual clock advances, or the
client's read timeout fires: ``httpx.ReadTimeout``, request processed); ``"status"`` replaces
the answer with an API error (HTTP 200); ``"http"`` with an HTTP error; ``"garbage"`` with a
body that is not JSON; ``"mutate"`` runs ``action(fake)`` first (delete a row between pages).

Webhooks: ``await deliver_webhook(fake, receiver_client, userid, appli, ...)`` POSTs what
Withings would POST to the subscribed callback URLs, with Withings' retry schedule when asked.
The HEAD check of ``notify subscribe`` goes to ``head_check`` (an async ``url -> status``;
``head_check_with(client)`` builds one from a client that reaches the receiver).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from withingscore import FakeWithings
from withingswire import (
    API_HOST,
    WEBHOOK_ATTEMPTS_PER_CYCLE,
    WEBHOOK_RETRY_DELAYS,
    App,
    ConnectionDropped,
    FakeUser,
    ManualClock,
    Request,
    Unroutable,
    webhook_form,
)


class FakeWithingsTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        fake: FakeWithings,
        *,
        head_check: Callable[[str], Awaitable[int]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.fake = fake
        self.head_check = head_check
        self._sleep = sleep

    async def _wait(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
        elif isinstance(self.fake.clock, ManualClock):
            self.fake.clock.advance(seconds)
        else:
            await asyncio.sleep(seconds)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        if request.url.scheme != "https":
            raise httpx.ConnectError("the Withings API is HTTPS only", request=request)
        core = Request(
            request.method,
            str(request.url),
            {name.lower(): value for name, value in request.headers.items()},
            body,
        )
        head_status = None
        callback = self.fake.callback_to_check(core)
        if callback is not None and self.head_check is not None:
            head_status = await self.head_check(callback)
        try:
            response = self.fake.handle(core, head_status=head_status)
        except Unroutable as error:
            raise httpx.ConnectError(f"no route to {error}", request=request) from None
        except ConnectionDropped as error:
            cls = httpx.ReadError if error.processed else httpx.ConnectError
            raise cls("connection dropped", request=request) from None
        if response.delay:
            read_timeout = request.extensions.get("timeout", {}).get("read")
            if read_timeout is not None and response.delay > read_timeout:
                await self._wait(read_timeout)  # the server processed it, the answer is late
                raise httpx.ReadTimeout("read timed out", request=request)
            await self._wait(response.delay)
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.body, request=request
        )


def make_fake(*, seed: int = 0, **options: Any) -> tuple[FakeWithings, App, FakeUser]:
    """A fake with one app and one synthetic user (``options`` go to ``generate_dataset``)."""
    fake = FakeWithings(seed=seed)
    return fake, fake.add_app(), fake.add_user(**options)


def fake_client(
    fake: FakeWithings, *, timeout: float | None = None, **options: Any
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose requests are answered by ``fake`` (no timeout unless given;
    ``options`` go to ``FakeWithingsTransport``)."""
    return httpx.AsyncClient(transport=FakeWithingsTransport(fake, **options), timeout=timeout)


def head_check_with(client: httpx.AsyncClient) -> Callable[[str], Awaitable[int]]:
    """A HEAD check for ``FakeWithingsTransport``: sends a real HEAD request through ``client``
    (for example an ``httpx.ASGITransport`` onto the webhook receiver)."""

    async def check(url: str) -> int:
        try:
            return (await client.head(url)).status_code
        except httpx.HTTPError:
            return 599

    return check


async def call(
    client: httpx.AsyncClient, path: str, action: str, token: str | None = None, **params: Any
) -> dict[str, Any]:
    """POST ``action`` to ``https://wbsapi.withings.net<path>``; returns the JSON envelope."""
    headers = {"authorization": f"Bearer {token}"} if token else {}
    data = {"action": action, **{k: str(v) for k, v in params.items()}}
    reply = await client.post(f"https://{API_HOST}{path}", data=data, headers=headers)
    return reply.json()


@dataclass
class Delivery:
    callbackurl: str
    attempt: int
    status_code: int | None  # None: no answer
    at: float


async def deliver_webhook(
    fake: FakeWithings,
    client: httpx.AsyncClient,
    userid: int,
    appli: int,
    *,
    retry: bool = False,
    form: dict[str, str] | None = None,
    **fields: Any,
) -> list[Delivery]:
    """POST the notification for ``(userid, appli)`` to every subscribed callback URL.

    ``fields`` go to ``webhook_form`` (``startdate``, ``enddate``, ``date``...); ``form``
    replaces the body (to send a forged or malformed one). Without ``retry`` there is one
    attempt. With it, Withings' schedule: 5 cycles at +0, +10 s, +1 min, +1 h and +4 h of 2
    attempts each, stopping at the first status below 400; the manual clock advances between
    cycles. A subscription that keeps failing for 20 days is cancelled (``record_delivery``).
    """
    body = form if form is not None else webhook_form(appli, userid, **fields)
    deliveries: list[Delivery] = []
    for sub in fake.webhook_targets(userid, appli):
        delays = WEBHOOK_RETRY_DELAYS if retry else ()
        ok = False
        for cycle in range(len(delays) + 1):
            for _ in range(WEBHOOK_ATTEMPTS_PER_CYCLE if retry else 1):
                try:
                    code: int | None = (await client.post(sub.callbackurl, data=body)).status_code
                except httpx.HTTPError:
                    code = None
                deliveries.append(
                    Delivery(sub.callbackurl, len(deliveries) + 1, code, fake.clock.now())
                )
                ok = code is not None and code < 400
                if ok:
                    break
            if ok:
                break
            if cycle < len(delays) and isinstance(fake.clock, ManualClock):
                fake.clock.advance(delays[cycle])
        fake.record_delivery(sub, ok)
    return deliveries
