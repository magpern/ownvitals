"""The total deadline of one urllib request (the network handler of ``ownvitals.egress.opener``).

``urlopen(timeout=30)`` is only a socket timeout per blocking operation, so a response trickling
in a byte at a time never ends on its own. The network handler (``DeadlineHTTPSHandler``) gives
every request a total deadline: its ``timeout`` (``REQUEST_FALLBACK_SECONDS`` when the caller gave
none), and never later than the thread's ``call_deadline`` if one is set. A watchdog shuts the
socket down when it passes, so a blocked read returns at once and the request raises
``RequestDeadline``; a request that would start after the deadline is refused before anything is
sent. The one wait it cannot cut is a DNS lookup in progress, which the resolver's own timeout
ends; the request is refused right after.
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

from ownvitals.egress.core import RequestDeadline

# A request whose caller gave no timeout (urllib's default) still ends within this.
REQUEST_FALLBACK_SECONDS: Final = 60.0
_thread_deadline = threading.local()


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
