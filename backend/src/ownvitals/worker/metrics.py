"""The worker's metrics (PLAN section 4.4 "Observability", #167): the ``/metrics`` listener and
the gauges refreshed from the database.

``metrics_running`` starts the listener (``ownvitals.metrics_server``) when ``metrics_bind`` is
set and a refresh task beside the claim loop, and stops both when the worker stops. The refresh
reads, in a **system session** (no user), the counts and ages of ``queue_stats()`` and of the
two ``SECURITY DEFINER`` aggregates of ``40-withings.sql``; those refuse a user session. It
writes nothing. Nothing it reads or sets carries an id, a user or a value.

Webhook delivery ages come from the rows the web process stored: the worker observes each age
once, keeping the last received time it saw (it starts at its own start time, so history is not
replayed on a restart). A notification committed late with an earlier received time than one
already seen is not counted.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.jobs import queue
from ownvitals.metrics_server import serve_metrics
from ownvitals.withings.metrics import apply_aggregates, record_webhook_age, set_queue

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]
_AGGREGATES = text("SELECT metric, stream, state, total FROM public.metrics_withings_aggregates()")
_AGES = text("SELECT received, age_seconds FROM public.metrics_webhook_ages(:after)")


class Refresher:
    """One refresh of the DB-derived gauges; keeps the webhook watermark between refreshes."""

    def __init__(self, factory: Factory, *, since: datetime | None = None) -> None:
        self._factory = factory
        self.after = since or datetime.now(UTC)

    async def refresh(self) -> None:
        async with system_session(self._factory) as db:
            rows = (await db.execute(_AGGREGATES)).all()
            ages = (await db.execute(_AGES, {"after": self.after})).all()
        oldest = apply_aggregates((r.metric, r.stream, r.state, float(r.total)) for r in rows)
        set_queue(dict(await queue.stats(self._factory)), oldest)
        for row in ages:
            record_webhook_age(float(row.age_seconds))
            self.after = max(self.after, row.received)


async def _refresh_until(refresher: Refresher, every: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await refresher.refresh()
        except Exception as exc:  # the next tick tries again; the claim loop is unaffected
            logger.warning("metrics refresh failed", extra={"error": type(exc).__name__})
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


@asynccontextmanager
async def metrics_running(
    settings: Settings, factory: Factory, stop: asyncio.Event
) -> AsyncIterator[Refresher]:
    """The listener (when configured) and the refresh task, for as long as the block runs. A
    refused bind address raises ``BindRefused`` before anything starts."""
    server = None
    if settings.metrics_bind is not None:
        server = await serve_metrics(settings.metrics_bind, settings.metrics_port)
    refresher = Refresher(factory)
    task = asyncio.ensure_future(_refresh_until(refresher, settings.metrics_refresh_seconds, stop))
    try:
        yield refresher
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if server is not None:
            server.close()
            await server.wait_closed()
