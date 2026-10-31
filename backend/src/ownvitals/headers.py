"""The security headers every web-process response carries (PLAN-operations §10.3).

- ``Content-Security-Policy``, as a response header (never a ``<meta>`` in ``index.html``):
  everything from our own origin only, with no exception. No inline or ``eval`` script, no inline
  ``<style>``, no plugins, no ``<base>``, forms only to us, and no framing by anyone. Images are
  ``'self'`` only, so nothing is fetched from a third party.
- ``Referrer-Policy: no-referrer``, page-wide, so no link or request carries an ownvitals URL
  (webhook paths and mail-link tokens must never leak through a ``Referer``).

``SecurityHeaders`` is a plain ASGI middleware, so it adds both to every HTTP response (the SPA,
its files, the API, errors and event streams) without buffering a body.
"""

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP_DIRECTIVES: Final = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "font-src 'self'",
    "connect-src 'self'",
    "media-src 'none'",
    "object-src 'none'",
    "frame-src 'none'",
    "worker-src 'none'",
    "manifest-src 'self'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
)
CONTENT_SECURITY_POLICY: Final = "; ".join(CSP_DIRECTIVES)
REFERRER_POLICY: Final = "no-referrer"


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                headers["Referrer-Policy"] = REFERRER_POLICY
            await send(message)

        await self.app(scope, receive, with_headers)
