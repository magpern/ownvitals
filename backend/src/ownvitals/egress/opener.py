"""The worker's process-wide urllib opener and the total deadline of one urllib request.

``install_worker_allowlist()`` installs, before anything else in the worker, an opener with an
empty ``ProxyHandler`` (no ``HTTPS_PROXY`` or ``NO_PROXY`` setting reroutes a call) and a guard
that refuses every request, redirect hops included, that is not ``https`` to a host on the
worker's list on port 443. It has no ``http``, ``ftp``, ``file`` or ``data`` handler, follows a
redirect only to the same host, and verifies TLS without a key log. ownvitals' own calls go
through the httpx clients (``ownvitals.egress.clients``); this opener catches any library that
reaches for ``urllib.request.urlopen`` in the worker.

**A total deadline per request.** ``urlopen(timeout=30)`` is only a socket timeout per blocking
operation, so a response trickling in a byte at a time never ends on its own. The network
handler (``DeadlineHTTPSHandler``) gives every request a total deadline: its ``timeout``
(``REQUEST_FALLBACK_SECONDS`` when the caller gave none), and never later than the thread's
``call_deadline`` if one is set. A watchdog shuts the socket down when it passes, so a blocked
read returns at once and the request raises ``RequestDeadline``; a request that would start after
the deadline is refused before anything is sent. The one wait it cannot cut is a DNS lookup in
progress, which the resolver's own timeout ends; the request is refused right after.
"""

import contextlib
import http.client
import math
import socket
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Final
from urllib.parse import urlsplit

from ownvitals.egress.core import (
    HTTPS_PORT,
    WORKER_ALLOWLIST,
    Allowlist,
    OutboundBlocked,
    RequestDeadline,
    _install_worker_list,  # the package's own; the guard refuses private names outside it
    blocked,
    spend,
    tls_client_context,
    worker_allowlist,
)
from ownvitals.egress.hook import _connection_scope  # the package's own

# A request whose caller gave no timeout (urllib's default) still ends within this.
REQUEST_FALLBACK_SECONDS: Final = 60.0
_thread_deadline = threading.local()


def _check_urllib_request(allowlist: Allowlist, req: urllib.request.Request) -> None:
    try:
        host = allowlist.check_url(req.full_url)
        # What http.client will actually connect to (``Request.host``), never through a proxy.
        if req.has_proxy() or req.host.lower() not in (host, f"{host}:{HTTPS_PORT}"):
            raise blocked(req.host)
    except OutboundBlocked as refused:
        # urllib's redirect handler gives every hop after the first a ``redirect_dict``.
        refused.after_request = hasattr(req, "redirect_dict")
        raise


class _AllowlistGuard(urllib.request.BaseHandler):
    """``default_open`` runs before any protocol handler, for every request and redirect hop."""

    handler_order = 0

    def __init__(self, allowlist: Allowlist) -> None:
        self._allowlist = allowlist

    def default_open(self, req: urllib.request.Request) -> None:
        _check_urllib_request(self._allowlist, req)
        spend(urlsplit(req.full_url).hostname)  # counted once per request or hop, before sending
        return None  # go on to https_open


def _same_host(old: str, new: str) -> bool:
    a, b = urlsplit(old), urlsplit(new)
    return (a.hostname or "").lower() == (b.hostname or "").lower()


class _SameHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to the same host: urllib copies every header (an ``Authorization``
    too) onto the next hop, so a hop to another listed host would hand it a token meant for the
    first. The new hop still goes through the guard."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if not _same_host(req.full_url, newurl):
            raise blocked(
                urlsplit(newurl).hostname if "://" in newurl else None, after_request=True
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# --- the total deadline of one urllib request ------------------------------------------------


@contextmanager
def call_deadline(seconds: float) -> Iterator[None]:
    """Inside the block, every urllib request this thread makes ends by ``now + seconds``: one
    still running then is cut off, and a later one is refused before it is sent. Nested blocks
    keep the earlier deadline."""
    if not seconds > 0:
        raise ValueError("a call deadline is positive")
    with call_deadline_until(time.monotonic() + seconds):
        yield


@contextmanager
def call_deadline_until(at: float) -> Iterator[None]:
    """``call_deadline`` for an absolute ``time.monotonic()`` instant taken by the caller before
    the call was scheduled. A thread that starts late uses up the time instead of extending it:
    once ``at`` has passed every request is refused before it is sent."""
    if not isinstance(at, int | float) or isinstance(at, bool):
        raise TypeError("a call deadline is a monotonic instant")
    if not math.isfinite(at):  # nan would compare false everywhere and never expire
        raise ValueError("a call deadline is a finite instant")
    previous: float | None = getattr(_thread_deadline, "at", None)
    _thread_deadline.at = float(at) if previous is None else min(previous, float(at))
    try:
        yield
    finally:
        _thread_deadline.at = previous


def request_deadline(timeout: object) -> float:
    """The monotonic time by which a request with this ``timeout`` must be over."""
    total = REQUEST_FALLBACK_SECONDS
    if (
        isinstance(timeout, int | float)
        and not isinstance(timeout, bool)
        and math.isfinite(timeout)
        and timeout > 0
    ):
        total = float(timeout)
    at = time.monotonic() + total
    call: float | None = getattr(_thread_deadline, "at", None)
    return at if call is None else min(at, call)


class _Watchdog:
    """Shuts the request's socket down at its deadline. It holds a duplicate of the socket
    (``shutdown`` acts on the connection, whatever descriptor it is called on, so it also cuts
    a TLS socket wrapped around the original), closed when the request is over."""

    def __init__(self, at: float) -> None:
        self.at = at
        self.expired = False
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._timer: threading.Timer | None = None
        self._done = False

    def check(self) -> None:
        if self.expired or time.monotonic() >= self.at:
            self.expired = True
            raise RequestDeadline

    def arm(self, sock: socket.socket) -> None:
        with self._lock:
            if self._done:
                return
            self._sock = sock.dup()
            timer = threading.Timer(max(0.0, self.at - time.monotonic()), self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            if self._done:
                return
            self.expired = True
            self._release(shutdown=True)

    def disarm(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._release(shutdown=False)

    def _release(self, *, shutdown: bool) -> None:  # with the lock held
        self._done = True
        sock, self._sock = self._sock, None
        if sock is None:
            return
        if shutdown:
            with contextlib.suppress(OSError):  # closed already
                sock.shutdown(socket.SHUT_RDWR)
        sock.close()

    def guard[T](self, read: Callable[[], T]) -> T:
        """A read of the response: past the deadline it raises ``RequestDeadline`` (also when
        the cut-off made the read fail or come back short)."""
        try:
            out = read()
        except Exception:
            if self.expired:
                raise RequestDeadline from None
            raise
        self.check()
        return out


class _DeadlineResponse(http.client.HTTPResponse):
    """A response whose reads honour the request's deadline; closing it ends the watchdog."""

    _watchdog: _Watchdog | None = None

    def watch(self, watchdog: _Watchdog) -> None:
        self._watchdog = watchdog

    def _guarded[T](self, read: Callable[[], T]) -> T:
        return read() if self._watchdog is None else self._watchdog.guard(read)

    def read(self, amt: int | None = None) -> bytes:
        return self._guarded(lambda: super(_DeadlineResponse, self).read(amt))

    def read1(self, n: int = -1) -> bytes:
        return self._guarded(lambda: super(_DeadlineResponse, self).read1(n))

    def readinto(self, b: Any) -> int:
        return self._guarded(lambda: super(_DeadlineResponse, self).readinto(b))

    def readline(self, limit: int = -1) -> bytes:
        return self._guarded(lambda: super(_DeadlineResponse, self).readline(limit))

    def peek(self, n: int = -1) -> bytes:
        return self._guarded(lambda: super(_DeadlineResponse, self).peek(n))

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._watchdog is not None:
                self._watchdog.disarm()


class DeadlineHTTPSConnection(http.client.HTTPSConnection):
    """``HTTPSConnection`` with the total deadline of its one request (urllib makes a
    connection per request): refused before connecting when it has passed, the watchdog armed
    as soon as the socket exists, and handed to the response."""

    response_class = _DeadlineResponse

    def __init__(self, host: str, *args: Any, **kwargs: Any) -> None:
        self._watchdog = _Watchdog(request_deadline(kwargs.get("timeout")))
        self._watchdog.check()  # nothing is sent after the deadline
        self._handing_over = False
        super().__init__(host, *args, **kwargs)
        create = self._create_connection

        def watched(*a: Any, **k: Any) -> socket.socket:
            sock = create(*a, **k)
            self._watchdog.arm(sock)
            return sock

        self._create_connection = watched

    def connect(self) -> None:
        try:
            super().connect()
            self._watchdog.check()  # the handshake ended past the deadline: send nothing
        except Exception:
            if self._watchdog.expired:
                raise RequestDeadline from None
            raise

    def send(self, data: Any) -> None:
        try:
            super().send(data)
        except Exception:
            if self._watchdog.expired:
                raise RequestDeadline from None
            raise

    def getresponse(self) -> http.client.HTTPResponse:
        # ``getresponse`` closes the connection itself when the response will close it, which
        # hands the socket to the response: the watchdog then ends with the response.
        self._handing_over = True
        try:
            response = super().getresponse()
        except Exception:
            self._watchdog.disarm()
            if self._watchdog.expired:
                raise RequestDeadline from None
            raise
        if isinstance(response, _DeadlineResponse):
            response.watch(self._watchdog)
        else:  # pragma: no cover - response_class is ours
            self._watchdog.disarm()
        return response

    def close(self) -> None:
        try:
            super().close()
        finally:
            if not self._handing_over:
                self._watchdog.disarm()


class DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib's HTTPS handler with ``DeadlineHTTPSConnection``: every request has a total
    deadline (module docstring)."""

    def https_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(DeadlineHTTPSConnection, req, context=self._context)  # pyright: ignore[reportAttributeAccessIssue]


class _AllowlistHTTPSHandler(DeadlineHTTPSHandler):
    """The network handler; checks again right before connecting."""

    def __init__(self, allowlist: Allowlist) -> None:
        super().__init__(context=tls_client_context())
        self._allowlist = allowlist

    def https_open(self, req: urllib.request.Request) -> Any:
        _check_urllib_request(self._allowlist, req)
        with _connection_scope("urllib"):  # the audit hook lets this request connect (ADR 0009)
            return super().https_open(req)


def build_allowlist_opener(
    allowlist: Allowlist = WORKER_ALLOWLIST,
    *,
    https_handler: urllib.request.BaseHandler | None = None,
) -> urllib.request.OpenerDirector:
    """An opener that only does ``https`` to ``allowlist``.

    Assembled by hand rather than with ``build_opener``, which would add the default ``http``,
    ``ftp``, ``file`` and ``data`` handlers and a ``ProxyHandler`` reading the environment.
    ``https_handler`` replaces the network handler in tests (no network); the guard still runs.
    """
    opener = urllib.request.OpenerDirector()
    handlers: tuple[urllib.request.BaseHandler, ...] = (
        # Empty: no proxy from the environment. With no proxies it registers no ``<scheme>_open``,
        # so OpenerDirector drops it, which is the point (a test checks no handler proxies).
        urllib.request.ProxyHandler({}),
        _AllowlistGuard(allowlist),
        https_handler if https_handler is not None else _AllowlistHTTPSHandler(allowlist),
        urllib.request.UnknownHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        _SameHostRedirectHandler(),  # same host only; each hop goes through the guard again
        urllib.request.HTTPErrorProcessor(),  # 4xx and 5xx raise HTTPError
    )
    for handler in handlers:
        opener.add_handler(handler)
    return opener


def install_worker_allowlist(*, ai_enabled: bool = False) -> urllib.request.OpenerDirector:
    """Make this process the worker: record its list (Withings; the model host only when
    ``ai_enabled``, the ``ai_insights`` setting) for the worker-only clients, and install its
    opener for every ``urllib.request.urlopen`` in the process. From now on the web-only call
    sites refuse (``refuse_in_worker``). Called first thing at worker start."""
    allowlist = worker_allowlist(ai_enabled=ai_enabled)
    opener = build_allowlist_opener(allowlist)
    urllib.request.install_opener(opener)
    _install_worker_list(allowlist)
    return opener
