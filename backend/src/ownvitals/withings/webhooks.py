"""The Withings webhook receiver and secret rotation (PLAN section 4.4, ADR 0008).

The receiver checks the request before the secret, then ``webhook_event_insert`` stores the
event and the singleton sync job in one transaction. The job names a connection and a stream;
the sync fetches from the API. Unknown and retired secrets get the same 200 and create nothing.
Rotation calls ``webhook_secrets_apply`` and ``queue_cancel_for_secret`` at READ COMMITTED.

The receiver and its middleware live in ``webhook_receiver.py`` and web's copy of the HMAC
entries in ``webhook_cache.py``; this module keeps the rotation and re-exports both.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.withings.subscriptions import (
    CurrentWebhookSeed,
    mint,
    pepper_hmac,
)
from ownvitals.withings.webhook_cache import Cache as Cache
from ownvitals.withings.webhook_cache import CachedEntry as CachedEntry
from ownvitals.withings.webhook_cache import Factory, _entries, _when
from ownvitals.withings.webhook_cache import WebhookTargets as WebhookTargets
from ownvitals.withings.webhook_cache import load_cache as load_cache
from ownvitals.withings.webhook_receiver import PREFIX as PREFIX
from ownvitals.withings.webhook_receiver import Metrics as Metrics
from ownvitals.withings.webhook_receiver import WebhookMiddleware as WebhookMiddleware

logger = logging.getLogger(__name__)

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
