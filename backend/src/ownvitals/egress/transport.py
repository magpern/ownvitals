"""``AllowlistTransport``: the httpx transport every client of ``ownvitals.egress.clients`` runs
on.

It checks every request, each redirect hop included, before the network transport sees it: the
process role (a worker-only client sends only in the worker, a web-only one never there), the
list (``https``, an exact host, port 443, no userinfo), and the request budget. It refuses a
redirect answer that points at another host: httpx drops ``Authorization`` on a cross-origin hop
but keeps other credential headers. With ``total_seconds`` each request, its answer's body
included, ends within that time (``RequestDeadline``); httpx's own timeouts are per read, so they
never end an answer that keeps trickling in.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Literal

import httpx

from ownvitals.egress.core import (
    Allowlist,
    OutboundBlocked,
    RequestDeadline,
    blocked,
    check_total_seconds,
    refuse_in_worker,
    require_worker_host,
    spend,
)
from ownvitals.egress.hook import _connection_scope  # the package's own

Role = Literal["web", "worker"]


class _DeadlineStream(httpx.AsyncByteStream):
    """An answer's body that must be read by ``at`` (``time.monotonic()``): each chunk waits at
    most until then, so a body trickling in a byte at a time ends with ``RequestDeadline``."""

    def __init__(self, inner: httpx.AsyncByteStream, at: float) -> None:
        self._inner = inner
        self._at = at

    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunks = self._inner.__aiter__()
        while True:
            remaining = self._at - time.monotonic()
            if remaining <= 0:
                raise RequestDeadline
            try:
                async with asyncio.timeout(remaining):
                    chunk = await anext(chunks)
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise RequestDeadline from None
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class AllowlistTransport(httpx.AsyncBaseTransport):
    """The checks of the module docstring around ``inner``. ``refused`` turns an
    ``OutboundBlocked`` into the exception a caller's library does not retry (raised ``from``
    it). ``role`` is ``"worker"`` (refused outside the worker and for hosts off its installed
    list), ``"web"`` (refused inside the worker) or None."""

    def __init__(
        self,
        allowlist: Allowlist,
        inner: httpx.AsyncBaseTransport,
        refused: Callable[[OutboundBlocked], Exception] | None = None,
        *,
        total_seconds: float | None = None,
        role: Role | None = None,
    ) -> None:
        self._allowlist = allowlist
        self._inner = inner
        self._refused = refused
        self._total_seconds = None if total_seconds is None else check_total_seconds(total_seconds)
        self._role = role

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            if self._total_seconds is None:
                return await self._handle(request)
            return await self._handle_within(request, time.monotonic() + self._total_seconds)
        except OutboundBlocked as exc:
            if self._refused is None:
                raise
            raise self._refused(exc) from exc

    async def _handle_within(self, request: httpx.Request, at: float) -> httpx.Response:
        try:
            async with asyncio.timeout(at - time.monotonic()):
                response = await self._handle(request)
        except TimeoutError:
            raise RequestDeadline from None
        stream = response.stream
        if not isinstance(stream, httpx.AsyncByteStream):  # pragma: no cover - async transports
            await response.aclose()
            raise TypeError("an async transport answers with an async stream")
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_DeadlineStream(stream, at),
            extensions=response.extensions,
            request=request,
        )

    def _check_role(self, host: str) -> None:
        if self._role == "web":
            refuse_in_worker(host)
        elif self._role == "worker":
            require_worker_host(host)

    async def _admit(self, request: httpx.Request) -> None:
        """A subclass's further checks of the request, before anything is sent."""

    def _redirect_allowed(self, url: httpx.URL, target: httpx.URL | None) -> bool:
        return target is not None and target.raw_host.lower() == url.raw_host.lower()

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host = url.raw_host.decode("ascii", errors="replace")
        self._check_role(host)
        self._allowlist.check(url.scheme, host, url.port, bool(url.userinfo))
        await self._admit(request)
        spend(host)  # before sending
        with _connection_scope("httpx"):  # the audit hook lets this request connect (ADR 0009)
            response = await self._inner.handle_async_request(request)
        location = response.headers.get("location")
        if response.is_redirect and location is not None:
            target: httpx.URL | None
            try:
                target = url.join(location)
            except (httpx.InvalidURL, ValueError):
                target = None
            if not self._redirect_allowed(url, target):
                await response.aclose()
                shown = None if target is None else target.raw_host.decode("ascii", "replace")
                raise blocked(shown, after_request=True)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()
