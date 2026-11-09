"""The Withings webhook receiver (PLAN section 4.4, ADR 0008).

The receiver checks the request before the secret, then ``webhook_event_insert`` stores the
event and the singleton sync job in one transaction. The job names a connection and a stream;
the sync fetches from the API. Unknown and retired secrets get the same 200 and create nothing;
one over quota gets it with the event recorded and no job. A matched secret whose notification
could not be stored gets a 500, never a 200 (durable before 200).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ownvitals.withings.webhook_cache import Cache, CachedEntry, Factory

# The logger keeps the name it had before this module was split out of ``webhooks.py``.
logger = logging.getLogger("ownvitals.withings.webhooks")


PREFIX = "/api/webhooks/withings/"
MAX_BODY = 2048
SECRET_MAX = 128
# 2009-01-01 UTC: the partition floor (ADR 0002 section 2). Dates after now + one day are refused.
EARLIEST = 1_230_768_000
FUTURE_SLACK = timedelta(days=1)


MEDIA = "application/x-www-form-urlencoded"
# PLAN section 4.4's appli column, one stream each. The same CASE is in webhook_event_insert.
APPLI_STREAM = {
    1: "measures",
    2: "measures",
    4: "measures",
    16: "activity_daily",
    44: "sleep_summary",
    46: "devices",
    54: "ecg",
    55: "ecg",
    58: "measures",
    62: "measures",
    63: "measures",
}
_INSERT = text("SELECT public.webhook_event_insert(:app, :user, :appli, :start, :end, :key, NULL)")
_OUTCOME = text("SELECT current_setting('ownvitals.webhook_outcome', true)")


@dataclass
class Metrics:
    ignored: int = 0
    alert_every: int = 50

    def ignore(self) -> None:
        self.ignored += 1
        if self.alert_every > 0 and self.ignored % self.alert_every == 0:
            logger.warning("webhook ignored rate", extra={"ignored": self.ignored})


def _cache(app: FastAPI) -> Cache:
    cache = getattr(app.state, "webhook_cache", None)
    return cache if isinstance(cache, Cache) else Cache()


def _metrics(app: FastAPI) -> Metrics:
    metrics = getattr(app.state, "webhook_metrics", None)
    if not isinstance(metrics, Metrics):
        metrics = Metrics()
        app.state.webhook_metrics = metrics
    return metrics


def _now(app: FastAPI) -> datetime:
    clock = getattr(app.state, "webhook_clock", None)
    return clock() if clock is not None else datetime.now(UTC)


def _media(header: str | None) -> str | None:
    if header is None:
        return None
    media, _, params = header.partition(";")
    if media.strip().lower() != MEDIA:
        return None
    for part in params.split(";"):
        name, _, value = part.strip().partition("=")
        if name.lower() == "charset" and value.strip().strip('"').lower() not in ("", "utf-8"):
            return None
    return MEDIA


def _fields(body: bytes, now: datetime) -> dict[str, int] | None:
    try:
        parsed = parse_qsl(body.decode("utf-8"), strict_parsing=True, keep_blank_values=False)
    except (UnicodeError, ValueError):
        return None
    if len(parsed) != 4 or len({name for name, _ in parsed}) != 4:
        return None
    form = dict(parsed)
    if set(form) != {"userid", "appli", "startdate", "enddate"}:
        return None
    numbers: dict[str, int] = {}
    for name, raw in form.items():
        if not raw.isdigit():
            return None
        numbers[name] = int(raw)
    user, appli = numbers["userid"], numbers["appli"]
    start, end = numbers["startdate"], numbers["enddate"]
    latest = int((now + FUTURE_SLACK).timestamp())
    if not (0 < user < 2**63 and appli in APPLI_STREAM and EARLIEST <= start <= end <= latest):
        return None
    return numbers


async def _body(request: Request) -> bytes | None:
    """The body, or None when it is over 2 KB. The extra byte is not kept."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY:
                return None
        except ValueError:
            return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


# webhook_event_insert raises 'duplicate' with this SQLSTATE for a replayed dedupe key.
DUPLICATE_SQLSTATE = "22023"


def _sqlstate(exc: BaseException) -> str | None:
    orig = exc.orig if isinstance(exc, DBAPIError) else None
    state = getattr(orig, "sqlstate", None)
    return state if isinstance(state, str) else None


def _is_duplicate(exc: BaseException) -> bool:
    """The replay error only: its SQLSTATE and its exact message, never a substring of
    another error's text."""
    if _sqlstate(exc) != DUPLICATE_SQLSTATE:
        return False
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "message_primary", None) == "duplicate"


def _log(request: Request, status: int) -> None:
    # The formatter redacts the path. The secret never reaches another field.
    logger.info("webhook", extra={"path": request.url.path, "status": status})


async def _ingest(factory: Factory, entry: CachedEntry, fields: Mapping[str, int]) -> str:
    key = (
        f"a{entry.app_id}.u{fields['userid']}.p{fields['appli']}"
        f".{fields['startdate']}.{fields['enddate']}"
    )
    async with factory() as session, session.begin():
        await session.execute(
            _INSERT,
            {
                "app": entry.app_id,
                "user": fields["userid"],
                "appli": fields["appli"],
                "start": fields["startdate"],
                "end": fields["enddate"],
                "key": key,
            },
        )
        outcome = (await session.execute(_OUTCOME)).scalar_one()
    return str(outcome or "queued")


async def handle(request: Request, presented: str) -> Response:
    """One notification. Checks run before the secret is compared, so their status does not
    say whether the secret is live."""
    now = _now(request.app)
    if request.method == "HEAD":
        _log(request, 200)
        return Response(status_code=200)
    if request.method != "POST":
        _log(request, 405)
        return Response(status_code=405, headers={"allow": "HEAD, POST"})
    if len(presented) > SECRET_MAX or _media(request.headers.get("content-type")) is None:
        _log(request, 415)
        return Response(status_code=415)
    body = await _body(request)
    if body is None:
        _log(request, 413)
        return Response(status_code=413)
    fields = _fields(body, now)
    if fields is None:
        _log(request, 400)
        return Response(status_code=400)
    cache = _cache(request.app)
    if not cache.fresh(now):
        _log(request, 503)
        return Response(status_code=503)
    entry = cache.match(presented, now)
    if entry is None:
        metrics = _metrics(request.app)
        metrics.ignore()
        _log(request, 200)
        return Response(status_code=200)
    try:
        outcome = await _ingest(request.app.state.session_factory, entry, fields)
    except Exception as exc:
        # A replay was stored before: the 200 stands. Anything else was not stored, so no 200
        # (PLAN section 4.4, durable before 200): a 500 that Withings retries. Only a holder of
        # a live 256-bit secret ever sees it, so it is no guessing oracle (ADR 0008, #280).
        if not _is_duplicate(exc):
            logger.warning(
                "webhook not stored",
                extra={"error_type": type(exc).__name__, "sqlstate": _sqlstate(exc)},
            )
            _log(request, 500)
            return Response(status_code=500)
        outcome = "duplicate"
    if outcome in ("unknown_user", "over_quota"):
        _note(request, outcome, entry.app_id)
    if getattr(request.app.state, "webhook_crash_after_commit", False):
        request.app.state.webhook_crash_after_commit = False
        _log(request, 500)
        return Response(status_code=500)
    _log(request, 200)
    return Response(status_code=200)


_NOTES = {
    "unknown_user": "webhook user is not linked",
    "over_quota": "webhook over quota",
}


def _note(request: Request, outcome: str, app_id: int) -> None:
    """At most one line a minute per outcome and app (PLAN section 4.4)."""
    seen: dict[tuple[str, int], datetime] = getattr(request.app.state, "webhook_noted_at", {})
    now = _now(request.app)
    previous = seen.get((outcome, app_id))
    if previous is not None and now - previous < timedelta(minutes=1):
        return
    seen[(outcome, app_id)] = now
    request.app.state.webhook_noted_at = seen
    logger.info(_NOTES[outcome], extra={"app_id": app_id})


class WebhookMiddleware(BaseHTTPMiddleware):
    """Serves the receiver before the auth middleware. Withings does not send a CSRF token."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(PREFIX):
            presented = path[len(PREFIX) :]
            if presented and "/" not in presented:
                return await handle(request, presented)
        return await call_next(request)
