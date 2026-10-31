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
The HEAD check of ``notify subscribe`` and ``update`` goes to ``head_check`` (an async
``url -> status``; ``head_check_with(client)`` builds one from a client that reaches the receiver).
It happens only once the request passed everything else (token, appli, scope, URL rules), just
before the subscription changes, so a request that fails anyway sends no HEAD.

Requests: the API takes ``POST`` with a form body only, and no secret in the query string
(``withingsrules``); a client that does otherwise gets an API error. The fake also holds an
application to 120 requests a minute (a token bucket on the fake clock, status 601; pass
``rate_limit=None`` to ``FakeWithings`` to switch it off).

Webhook senders: only a 2xx answer counts, within ``WEBHOOK_RESPONSE_TIMEOUT`` seconds (real time,
or the fake clock when the receiver advances it); redirects are not followed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from withingscore import FakeWithings
from withingsnotify import HeadCheck
from withingswire import (
    API_HOST,
    WEBHOOK_ATTEMPTS_PER_CYCLE,
    WEBHOOK_RESPONSE_TIMEOUT,
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
        try:
            step = self.fake.start(core)
            if isinstance(step, HeadCheck):  # notify takes the callback URL if this answers 2xx
                head = self.head_check
                step = step.finish(
                    self.fake.default_head_status if head is None else await head(step.url)
                )
            response = step
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
    status_code: int | None  # None: no answer, or none within the response timeout
    at: float
    timed_out: bool = False  # the receiver answered (or not) after the response timeout


async def _post_webhook(
    fake: FakeWithings, client: httpx.AsyncClient, url: str, body: dict[str, str], within: float
) -> tuple[int | None, bool]:
    """POST once; ``(status, timed_out)``. Only an answer within ``within`` seconds counts, whether
    the receiver is slow in real time (``asyncio.wait_for``) or advances the fake clock."""
    started = fake.clock.now()
    try:
        reply = await asyncio.wait_for(client.post(url, data=body, follow_redirects=False), within)
    except TimeoutError:
        return None, True
    except httpx.HTTPError:
        return None, False
    if fake.clock.now() - started > within:
        return None, True
    return reply.status_code, False


async def deliver_webhook(
    fake: FakeWithings,
    client: httpx.AsyncClient,
    userid: int,
    appli: int,
    *,
    retry: bool = False,
    form: dict[str, str] | None = None,
    response_timeout: float = WEBHOOK_RESPONSE_TIMEOUT,
    **fields: Any,
) -> list[Delivery]:
    """POST the notification for ``(userid, appli)`` to every subscribed callback URL.

    ``fields`` go to ``webhook_form`` (``startdate``, ``enddate``, ``date``...); ``form``
    replaces the body (to send a forged or malformed one). Success is a 2xx within
    ``response_timeout`` seconds (findings section 2: "2xx within a few seconds"); a 3xx, 4xx,
    5xx, a connection error or a late answer fails the attempt (redirects are not followed). The
    receiver may have processed a late request all the same, as with Withings. Without ``retry``
    there is one attempt. With it, Withings' schedule: 5 cycles at +0, +10 s, +1 min, +1 h and
    +4 h of 2 attempts each, stopping at the first success; the manual clock advances between
    cycles. A subscription that keeps failing for 20 days is cancelled (``record_delivery``).
    """
    body = form if form is not None else webhook_form(appli, userid, **fields)
    deliveries: list[Delivery] = []
    for sub in fake.webhook_targets(userid, appli):
        delays = WEBHOOK_RETRY_DELAYS if retry else ()
        ok = False
        for cycle in range(len(delays) + 1):
            for _ in range(WEBHOOK_ATTEMPTS_PER_CYCLE if retry else 1):
                code, late = await _post_webhook(
                    fake, client, sub.callbackurl, body, response_timeout
                )
                deliveries.append(
                    Delivery(sub.callbackurl, len(deliveries) + 1, code, fake.clock.now(), late)
                )
                ok = code is not None and 200 <= code < 300
                if ok:
                    break
            if ok:
                break
            if cycle < len(delays) and isinstance(fake.clock, ManualClock):
                fake.clock.advance(delays[cycle])
        fake.record_delivery(sub, ok)
    return deliveries
