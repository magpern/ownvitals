"""``/api/unsubscribe``: the signed one-click unsubscribe (RFC 8058; ADR 0018; #459). Web only.

Unauthenticated by design: a mail client posts to the ``List-Unsubscribe`` URL with no cookie,
no ``Origin`` and no CSRF token, so the endpoint is served by ``UnsubscribeMiddleware``, outside
the auth middleware (``ownvitals.web`` mounts it next to the webhook receiver's). Its only
authority is the token in the query string (``?t=``):

- ``GET`` (and ``HEAD``) answers the confirm page and changes nothing: no token check, no
  database, no rate-limit slot, the same page for every token;
- ``POST`` takes a slot of the per-address limit (``ownvitals.auth.ratelimit.UNSUBSCRIBE_IP``,
  20 per 15 minutes; above it 429 and
  ``Retry-After``), verifies the token (``ownvitals.notify.unsubscribe.verify``) and, for an
  authentic unexpired one, calls ``email_unsubscribe`` with exactly its owner, kind and
  generation (``ownvitals.notify.outbox.unsubscribe``): the outbox family's reviewed definer
  function, which switches that one suppressible kind off only while it is on at that
  generation. It never reads the body. It answers the same page with 200 for a valid, expired,
  forged, stale or repeated token, so the answer is no oracle;
- any other method is 405.

Never logged: the token, the owner, the kind. The access log is the auth middleware's and does not
see these requests; this module logs only whether a request changed a preference. Every answer is
``no-store``; the outer ``SecurityHeaders`` add the CSP and ``Referrer-Policy: no-referrer``, so
the page leaks the URL nowhere.
"""

import logging
from datetime import UTC, datetime
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ownvitals.auth import recipients
from ownvitals.auth.deps import reserve_unsubscribe
from ownvitals.config import MIN_APP_SECRET_BYTES, Settings
from ownvitals.notify import outbox, unsubscribe, unsubscribe_page

logger = logging.getLogger(__name__)

_HEADERS: Final = {"Cache-Control": "no-store"}


def key_for(settings: Settings) -> bytes:
    """The token key from ``app_secret_key`` (web refuses to start without it, ADR 0003)."""
    secret = settings.app_secret_key
    if secret is None or len(secret.get_secret_value().encode()) < MIN_APP_SECRET_BYTES:
        raise RuntimeError("the app_secret_key secret file is missing or too short")
    return unsubscribe.unsubscribe_key(secret.get_secret_value().encode())


def install(app: FastAPI, settings: Settings) -> None:
    """Derive the token key into ``app.state`` (web's lifespan, before the sender starts)."""
    app.state.unsubscribe_key = key_for(settings)


def _html(body: str, status: int = 200, extra: dict[str, str] | None = None) -> Response:
    return HTMLResponse(body, status_code=status, headers={**_HEADERS, **(extra or {})})


async def _post(request: Request, locale: str) -> Response:
    state = request.app.state
    retry_after = await reserve_unsubscribe(request)  # every POST counts (ADR 0018)
    if retry_after is not None:
        retry = {"Retry-After": str(retry_after)}
        return _html(unsubscribe_page.limited(locale), 429, retry)
    claim = unsubscribe.verify(
        state.unsubscribe_key, request.query_params.get(unsubscribe.QUERY), datetime.now(UTC)
    )
    changed = False
    if claim is not None:
        changed = await outbox.unsubscribe(
            state.session_factory, claim.owner_id, claim.kind, claim.generation
        )
    logger.info("unsubscribe request", extra={"changed": changed})
    return _html(unsubscribe_page.done(locale))


async def handle(request: Request) -> Response:
    locale = recipients.request_locale(request)
    if request.method in ("GET", "HEAD"):
        token = request.query_params.get(unsubscribe.QUERY)
        return _html(unsubscribe_page.confirm(locale, token))
    if request.method == "POST":
        return await _post(request, locale)
    return Response(status_code=405, headers={**_HEADERS, "Allow": "GET, HEAD, POST"})


class UnsubscribeMiddleware(BaseHTTPMiddleware):
    """Serves ``/api/unsubscribe`` before the auth middleware: a one-click client sends no
    ``Origin`` and no CSRF token. Every other path passes through."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path == unsubscribe.PATH:
            return await handle(request)
        return await call_next(request)
