"""The Withings webhook receiver and secret rotation (PLAN section 4.4, ADR 0008).

The receiver checks the request before the secret, then ``webhook_event_insert`` stores the
event and the singleton sync job in one transaction. The job names a connection and a stream;
the sync fetches from the API. Unknown and retired secrets get the same 200 and create nothing.
Rotation calls ``webhook_secrets_apply`` and ``queue_cancel_for_secret`` at READ COMMITTED.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from ownvitals.crypto.keks import KeyUnavailable
from ownvitals.withings.subscriptions import (
    CurrentWebhookSeed,
    mint,
    pepper_hmac,
)

logger = logging.getLogger(__name__)

PREFIX = "/api/webhooks/withings/"
MAX_BODY = 2048
SECRET_MAX = 128
# 2009-01-01 UTC: the partition floor (ADR 0002 section 2). Dates after now + one day are refused.
EARLIEST = 1_230_768_000
FUTURE_SLACK = timedelta(days=1)
CACHE_TTL = timedelta(seconds=60)
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
_APPS = text("SELECT id, webhook_secrets FROM public.withings_apps")
_APP = text(
    "SELECT webhook_secrets, rotation_requested_at, rotation_reason "
    "FROM public.withings_apps WHERE id = :id"
)
_APPLY = text(
    "SELECT public.webhook_secrets_apply(:app, :op, :secret, :seed, :pepper, :hmac, "
    ":active, :seen_at, :seen_reason)"
)
_CANCEL = text("SELECT public.queue_cancel_for_secret(:kind, :secret)")
_LOCK = text("SELECT pg_catalog.pg_advisory_xact_lock(4, :app)")
_TRY_LOCK = text("SELECT pg_catalog.pg_try_advisory_xact_lock(4, :app)")
Factory = async_sessionmaker[AsyncSession]


@dataclass
class CachedEntry:
    app_id: int
    secret_id: str
    digest: bytes
    pepper: bytes
    active_from: datetime
    retire_at: datetime | None

    def __repr__(self) -> str:
        return f"CachedEntry(app_id={self.app_id}, secret_id={self.secret_id!r})"

    def open_at(self, now: datetime) -> bool:
        return self.retire_at is None and self.active_from <= now


@dataclass
class Cache:
    """Web's copy of the HMAC entries. ``read_at`` is the start of the read that made it."""

    entries: list[CachedEntry] = field(default_factory=list)
    read_at: datetime | None = None
    ttl: timedelta = CACHE_TTL

    def fresh(self, now: datetime) -> bool:
        return self.read_at is not None and now - self.read_at <= self.ttl

    def match(self, presented: str, now: datetime) -> CachedEntry | None:
        found: CachedEntry | None = None
        try:
            token = presented.encode("ascii")
        except UnicodeError:
            return None
        for entry in self.entries:
            if not entry.open_at(now) or len(entry.pepper) != 32 or len(entry.digest) != 32:
                continue
            candidate = hmac.new(entry.pepper, token, hashlib.sha256).digest()
            if hmac.compare_digest(candidate, entry.digest):
                found = entry
        return found


@dataclass
class Metrics:
    ignored: int = 0
    alert_every: int = 50

    def ignore(self) -> None:
        self.ignored += 1
        if self.alert_every > 0 and self.ignored % self.alert_every == 0:
            logger.warning("webhook ignored rate", extra={"ignored": self.ignored})


def _entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _when(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class WebhookTargets:
    """Whether a webhook secret is still an active job target (ADR 0008). Fail closed."""

    async def entry_active(self, factory: Factory, secret_id: UUID) -> bool:
        now = datetime.now(UTC)
        async with factory() as session, session.begin():
            rows = (await session.execute(_APPS)).all()
        for row in rows:
            for item in _entries(row.webhook_secrets):
                if item.get("secret_id") != str(secret_id):
                    continue
                opened = _when(item.get("active_from"))
                if item.get("retire_at") is None and opened is not None and opened <= now:
                    return True
        return False


async def load_cache(app: FastAPI) -> None:
    """Read the HMAC entries once. A miss leaves the previous copy, or none yet."""
    factory: Factory = app.state.session_factory
    ring = app.state.keyring
    started = datetime.now(UTC)
    peppers: dict[str, bytes] = {}
    entries: list[CachedEntry] = []
    async with factory() as session, session.begin():
        rows = (await session.execute(_APPS)).all()
        for row in rows:
            for item in _entries(row.webhook_secrets):
                pepper_id = str(item.get("pepper_id") or "")
                if pepper_id not in peppers:
                    try:
                        peppers[pepper_id] = (await ring.pepper_for(session, pepper_id)).key
                    except KeyUnavailable:
                        peppers[pepper_id] = b""
                opened = _when(item.get("active_from"))
                digest = bytes.fromhex(str(item.get("hmac") or ""))
                if opened is None or len(digest) != 32:
                    continue
                entries.append(
                    CachedEntry(
                        int(row.id),
                        str(item.get("secret_id")),
                        digest,
                        peppers[pepper_id],
                        opened,
                        _when(item.get("retire_at")),
                    )
                )
    cache = getattr(app.state, "webhook_cache", None)
    if not isinstance(cache, Cache):
        cache = Cache()
        app.state.webhook_cache = cache
    cache.entries = entries
    cache.read_at = started


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
    except DBAPIError as exc:
        if "duplicate" not in str(exc):
            logger.warning("webhook not stored", extra={"error_type": type(exc).__name__})
            _log(request, 500)
            return Response(status_code=500)
        outcome = "duplicate"
    if outcome == "unknown_user":
        _note_unknown(request, entry.app_id)
    if getattr(request.app.state, "webhook_crash_after_commit", False):
        request.app.state.webhook_crash_after_commit = False
        _log(request, 500)
        return Response(status_code=500)
    _log(request, 200)
    return Response(status_code=200)


def _note_unknown(request: Request, app_id: int) -> None:
    seen: dict[int, datetime] = getattr(request.app.state, "webhook_unknown_at", {})
    now = _now(request.app)
    previous = seen.get(app_id)
    if previous is not None and now - previous < timedelta(minutes=1):
        return
    seen[app_id] = now
    request.app.state.webhook_unknown_at = seen
    logger.info("webhook user is not linked", extra={"app_id": app_id})


class WebhookMiddleware(BaseHTTPMiddleware):
    """Serves the receiver before the auth middleware. Withings does not send a CSRF token."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(PREFIX):
            presented = path[len(PREFIX) :]
            if presented and "/" not in presented:
                return await handle(request, presented)
        return await call_next(request)


async def _apply(
    session: AsyncSession, app_id: int, operation: str, row: Any, **values: Any
) -> None:
    await session.execute(
        _APPLY,
        {
            "app": app_id,
            "op": operation,
            "secret": values.get("secret"),
            "seed": values.get("seed"),
            "pepper": values.get("pepper"),
            "hmac": values.get("hmac"),
            "active": values.get("active"),
            "seen_at": row.rotation_requested_at,
            "seen_reason": row.rotation_reason,
        },
    )


async def append_secret(
    factory: Factory,
    *,
    app_id: int,
    seed: CurrentWebhookSeed,
    pepper: bytes,
    pepper_id: str,
    active_from: datetime,
) -> str:
    """Append one entry. ``active_from`` is at least the cache TTL ahead for a real rotation."""
    secret_id = uuid4()
    presented = mint(seed, app_id, secret_id)
    session = factory()
    async with session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        await session.execute(_LOCK, {"app": app_id})
        row = (await session.execute(_APP, {"id": app_id})).one()
        await _apply(
            session,
            app_id,
            "append",
            row,
            secret=str(secret_id),
            seed=seed.key_id,
            pepper=pepper_id,
            hmac=pepper_hmac(pepper, presented),
            active=active_from,
        )
    return presented


async def retire_secret(factory: Factory, *, app_id: int, secret_id: UUID) -> None:
    session = factory()
    async with session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        await session.execute(_LOCK, {"app": app_id})
        row = (await session.execute(_APP, {"id": app_id})).one()
        await _apply(session, app_id, "retire", row, secret=str(secret_id))


async def retire_all(factory: Factory, *, app_id: int) -> int:
    """Compromise step: retire every live entry and cancel its queued re-point jobs."""
    session = factory()
    async with session, session.begin():
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        await session.execute(_LOCK, {"app": app_id})
        row = (await session.execute(_APP, {"id": app_id})).one()
        live = [
            str(item["secret_id"])
            for item in _entries(row.webhook_secrets)
            if item.get("retire_at") is None and item.get("secret_id")
        ]
        await _apply(session, app_id, "retire-all", row)
        cancelled = 0
        for secret_id in live:
            cancelled += (
                await session.execute(_CANCEL, {"kind": "notify_update", "secret": UUID(secret_id)})
            ).scalar_one()
    if cancelled:
        logger.warning(
            "webhook rotation stalled",
            extra={"alert": "notify_update_cancelled", "cancelled": cancelled},
        )
    return cancelled


async def rotation_stalls(factory: Factory, app_id: int, *, bound: timedelta) -> set[str]:
    """Alerts when a request is past ``bound``, or two live entries overlap and the newest
    ``active_from`` is at least ``bound`` in the past, including when lock 4 is held."""
    alerts: set[str] = set()
    now = datetime.now(UTC)
    async with factory() as session, session.begin():
        acquired = (await session.execute(_TRY_LOCK, {"app": app_id})).scalar_one()
        row = (await session.execute(_APP, {"id": app_id})).one()
        requested = _when(row.rotation_requested_at)
        pending = requested is not None and now - requested >= bound
        if not acquired and pending:
            alerts.add("rotation_locked")
        if pending and acquired:
            alerts.add("request_pending")
        live = [
            item
            for item in _entries(row.webhook_secrets)
            if item.get("retire_at") is None and _when(item.get("active_from")) is not None
        ]
        # The original secret stays live for the whole overlap, so its age is not the stall.
        # Alert only once the newest secret's active_from is at least ``bound`` in the past.
        opened = [_when(item.get("active_from")) for item in live]
        newest = max((stamp for stamp in opened if stamp is not None), default=None)
        if len(live) >= 2 and newest is not None and now - newest >= bound:
            alerts.add("step_late")
    for code in sorted(alerts):
        logger.warning("webhook rotation stalled", extra={"alert": code, "app_id": app_id})
    return alerts
