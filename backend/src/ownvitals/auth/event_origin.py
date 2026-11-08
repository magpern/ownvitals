"""Where an ``auth_events`` row was written from, as coarse display data (#251).

Every row written while web handles a request gets the request's browser family and operating
system (``useragent``, a fixed set or ``other``) and its place (``geo``, ``None`` for "Place
unknown"). Never the address, the ``User-Agent`` string or a header value: only these three.
A row written outside a request (the worker's purge, the operator CLI) gets none.

``EventOriginMiddleware`` (installed by ``setup.install``) puts the request in a context
variable for the time the request runs; ``record_event`` asks ``current_origin`` and works the
three values out only then. The values are display data, never part of a security decision.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from ownvitals.auth import geo
from ownvitals.auth.useragent import describe

BROWSER_MAX: Final = 32  # the column checks of 0019_auth_event_origin
SYSTEM_MAX: Final = 32

_request: ContextVar[Request | None] = ContextVar("ownvitals_auth_event_request", default=None)


@dataclass(frozen=True, slots=True)
class EventOrigin:
    browser: str  # one of ``useragent.BROWSERS``
    os: str  # one of ``useragent.SYSTEMS``
    place: str | None  # ``geo`` display text, at most ``geo.PLACE_MAX``; None is unknown


def origin_of(request: Request) -> EventOrigin:
    """The request's coarse origin. Only the first ``useragent.UA_MAX`` characters of the header
    are looked at, and only one of the fixed names is kept."""
    values = request.headers.getlist("user-agent")
    browser, system = describe(values[0] if len(values) == 1 else None)
    return EventOrigin(browser, system, geo.place(request, request.app.state.settings))


def current_origin() -> EventOrigin | None:
    """The origin of the request being handled, or None outside a request."""
    request = _request.get()
    return None if request is None else origin_of(request)


class EventOriginMiddleware:
    """Make the HTTP request being handled the one ``current_origin`` describes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _request.set(Request(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            _request.reset(token)
