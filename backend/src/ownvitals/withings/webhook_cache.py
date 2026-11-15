"""Web's copy of the webhook secrets' HMAC entries (PLAN section 4.4, ADR 0004, ADR 0008).

The receiver (``webhook_receiver.py``) compares a presented secret only against this copy;
``load_cache`` reads it and ``webhook_refresh`` re-reads it well within its TTL.
``WebhookTargets`` tells the worker whether a secret is still an active job target. Secrets
are never stored: only their HMACs and the pepper key ids.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.crypto.keks import KeyUnavailable

CACHE_TTL = timedelta(seconds=60)


_APPS = text("SELECT id, webhook_secrets FROM public.withings_apps")


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


def app_now(app: FastAPI) -> datetime:
    """The receiver's and the cache's clock: ``app.state.webhook_clock`` (tests), else UTC now."""
    clock = getattr(app.state, "webhook_clock", None)
    return clock() if clock is not None else datetime.now(UTC)


async def load_cache(app: FastAPI) -> None:
    """Read the HMAC entries once. A miss leaves the previous copy, or none yet. ``read_at`` is
    stamped with the receiver's clock, and an existing ``Cache`` keeps its ``ttl``."""
    factory: Factory = app.state.session_factory
    ring = app.state.keyring
    started = app_now(app)
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
