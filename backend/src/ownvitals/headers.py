"""The security headers every web-process response carries (PLAN-operations §10.3).

- ``Content-Security-Policy``, as a response header (never a ``<meta>`` in ``index.html``):
  everything from our own origin only, with no exception. No inline or ``eval`` script, no inline
  ``<style>``, no plugins, no ``<base>``, forms only to us, and no framing by anyone. Images are
  ``'self'`` only, so nothing is fetched from a third party. One conditional exception: with
  ``turnstile_enabled`` (ADR 0018) ``script-src`` and ``frame-src`` also admit Turnstile's
  origin, for the widget on the sign-in, sign-up and reset forms; off, the policy is unchanged.
- ``Referrer-Policy: no-referrer``, page-wide, so no link or request carries an ownvitals URL
  (webhook paths and mail-link tokens must never leak through a ``Referer``).
- ``X-Content-Type-Options: nosniff``, so a browser never guesses a type for a file we served.

``SecurityHeaders`` is a plain ASGI middleware, so it adds all three to every HTTP response (the
SPA, its files, the API, errors and event streams) without buffering a body.
"""

from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ownvitals.egress import TURNSTILE_HOST

TURNSTILE_ORIGIN: Final = f"https://{TURNSTILE_HOST}"

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
# The directives Turnstile's widget needs (its script and its iframe), replacing today's.
_TURNSTILE_DIRECTIVES: Final = {
    "script-src 'self'": f"script-src 'self' {TURNSTILE_ORIGIN}",
    "frame-src 'none'": f"frame-src {TURNSTILE_ORIGIN}",
}
TURNSTILE_CONTENT_SECURITY_POLICY: Final = "; ".join(
    _TURNSTILE_DIRECTIVES.get(d, d) for d in CSP_DIRECTIVES
)


def content_security_policy(scope: Scope) -> str:
    """The policy for this app: Turnstile's only when its settings say ``turnstile_enabled`` is
    exactly true; anything else (no app, no settings) gets the strict one."""
    state = getattr(scope.get("app"), "state", None)
    settings = getattr(state, "settings", None)
    if getattr(settings, "turnstile_enabled", False) is True:
        return TURNSTILE_CONTENT_SECURITY_POLICY
    return CONTENT_SECURITY_POLICY


REFERRER_POLICY: Final = "no-referrer"
CONTENT_TYPE_OPTIONS: Final = "nosniff"


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        policy = content_security_policy(scope)

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Content-Security-Policy"] = policy
                headers["Referrer-Policy"] = REFERRER_POLICY
                headers["X-Content-Type-Options"] = CONTENT_TYPE_OPTIONS
            await send(message)

        await self.app(scope, receive, with_headers)
