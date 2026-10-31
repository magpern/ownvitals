"""The httpx clients: every request ownvitals makes over HTTPS goes through ``AllowlistTransport``.

The transport checks every request, each redirect hop included, before the network transport
sees it: the process role (a worker-only client sends only in the worker, a web-only one never
there), the list (``https``, an exact host, port 443, no userinfo), and the request budget. It
refuses a redirect answer that points at another host: httpx drops ``Authorization`` on a
cross-origin hop but keeps other credential headers. With ``total_seconds`` each request, its
answer's body included, ends within that time (``RequestDeadline``); httpx's own timeouts are per
read, so they never end an answer that keeps trickling in.

Every client has ``trust_env=False`` (no proxy or ``NETRC`` from the environment), does not
follow redirects, and verifies TLS against ``certifi`` (httpx's own default) with a context that
never writes a key log. ``inner`` replaces the network transport in tests (an
``httpx.MockTransport`` or a fake, no network); every check still runs. No client takes a list:
each one's list is chosen here.

| Client | Process | Reaches |
|---|---|---|
| ``withings_async_client`` | worker | ``wbsapi.withings.net``, ``account.withings.com`` |
| ``model_async_client`` | worker, AI enabled | ``api.anthropic.com`` |
| ``google_signin_transport``, ``google_async_client`` | web | Google's token endpoint and JWKS |
| ``withings_code_exchange_client`` (``code_exchange``) | web | one ``POST`` (roadmap Amendment 1) |
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Final, Literal

import certifi
import httpx

from ownvitals.egress.core import (
    GOOGLE_SIGNIN_ALLOWLIST,
    GOOGLE_TOKEN_HOST,
    MODEL_ALLOWLIST,
    MODEL_HOST,
    WITHINGS_API_HOST,
    WITHINGS_HOSTS,
    Allowlist,
    OutboundBlocked,
    RequestDeadline,
    blocked,
    check_total_seconds,
    refuse_in_worker,
    require_worker_host,
    spend,
    tls_client_context,
    worker_only,
)

Role = Literal["web", "worker"]
MODEL_TIMEOUT_SECONDS: Final = 120.0
# Total deadline of one Withings API request unless the caller gives one.
WITHINGS_TOTAL_SECONDS: Final = 30.0
# Total deadline of one Google sign-in request (classbeat's 10 s OAuth timeout).
GOOGLE_TOTAL_SECONDS: Final = 10.0


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


def _network_transport() -> httpx.AsyncHTTPTransport:
    """The network transport under the checks: trust store ``certifi``, no TLS key log, no proxy.
    ``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` are ignored because the context is handed in."""
    return httpx.AsyncHTTPTransport(
        trust_env=False, verify=tls_client_context(cafile=certifi.where())
    )


def _client(transport: AllowlistTransport, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport, trust_env=False, timeout=timeout, follow_redirects=False
    )


def withings_async_client(
    *,
    total_seconds: float = WITHINGS_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The worker's Withings client (PLAN §4.2): every Withings call but the web process's
    authorization-code exchange. Refused outside the worker (``OutboundBlocked``), so the web
    process can make no refresh, revoke or other Withings call with it. Every request with its
    body ends within ``total_seconds``."""
    seconds = check_total_seconds(total_seconds)
    allowlist = worker_only(WITHINGS_HOSTS, WITHINGS_API_HOST)
    network = inner if inner is not None else _network_transport()
    return _client(
        AllowlistTransport(allowlist, network, total_seconds=seconds, role="worker"), seconds
    )


def model_async_client(
    *,
    timeout: float = MODEL_TIMEOUT_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
    refused: Callable[[OutboundBlocked], Exception] | None = None,
) -> httpx.AsyncClient:
    """The model SDK's client (M7: ``AsyncAnthropic(http_client=...)``). Built only in a worker
    whose list holds the model host, that is with AI enabled; otherwise ``OutboundBlocked``. Its
    list is the model host alone, although the worker's is wider."""
    allowlist = worker_only(MODEL_ALLOWLIST.hosts, MODEL_HOST)
    network = inner if inner is not None else _network_transport()
    return _client(AllowlistTransport(allowlist, network, refused, role="worker"), timeout)


def google_signin_transport(
    *,
    total_seconds: float = GOOGLE_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> AllowlistTransport:
    """The transport of Google sign-in (web only): Authlib's ``AsyncOAuth2Client`` takes it as
    ``transport=`` with ``trust_env=False`` (the guard requires both). Its list is Google's token
    endpoint and JWKS hosts; refused in the worker."""
    seconds = check_total_seconds(total_seconds)
    refuse_in_worker(GOOGLE_TOKEN_HOST)
    network = inner if inner is not None else _network_transport()
    return AllowlistTransport(GOOGLE_SIGNIN_ALLOWLIST, network, total_seconds=seconds, role="web")


def google_async_client(
    *,
    total_seconds: float = GOOGLE_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` over ``google_signin_transport`` (the JWKS fetch; web only)."""
    seconds = check_total_seconds(total_seconds)
    return _client(google_signin_transport(total_seconds=seconds, inner=inner), seconds)
