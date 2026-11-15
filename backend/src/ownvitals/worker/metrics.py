"""The worker's metrics (PLAN section 4.4 "Observability", #167): the ``/metrics`` listener and
the gauges refreshed from the database.

``metrics_running`` starts the listener (``ownvitals.metrics_server``) when ``metrics_bind`` is
set and a refresh task beside the claim loop, and stops both when the worker stops. The task
refreshes only once the engine's queue check has passed (``queue_verified``, ADR 0005 section
3): a database that was down at startup, or a drifted queue, gets no definer call (#303). The
refresh reads, in a **system session** (no user), the counts and ages of ``queue_stats()`` and
of the two ``SECURITY DEFINER`` aggregates of ``40-withings.sql``; those refuse a user session.
It writes nothing. Nothing it sets carries an id, a user or a value; the one id it reads is a
notification row's, kept only as the watermark's tie-break.

Webhook delivery ages come from the rows the web process stored: the worker observes each age
once, keeping the watermark (received time, row id) of the last row it saw (it starts at its own
start time, so history is not replayed on a restart). Pages of ``AGES_PAGE`` rows are read until
one is short, at most ``AGES_PAGES`` per refresh; the rest waits for the next one. A burst, or
rows with equal received times, are each counted exactly once (#303). The watermark moves only
after the whole read succeeded. A notification committed late with an earlier received time
than one already seen is not counted.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.db import system_session
from ownvitals.db.queue_check import queue_verified
from ownvitals.jobs import queue
from ownvitals.metrics_server import serve_metrics
from ownvitals.withings.metrics import apply_aggregates, record_webhook_age, set_queue

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]
_AGGREGATES = text("SELECT metric, stream, state, total FROM public.metrics_withings_aggregates()")
_AGES = text(
    "SELECT received, event_id, age_seconds FROM public.metrics_webhook_ages(:after, :after_id)"
)
AGES_PAGE = 1000  # the LIMIT of metrics_webhook_ages()
AGES_PAGES = 10  # pages per refresh; a longer backlog continues on the next refresh
_START_ID = uuid.UUID(int=0)  # below every id: the start time's rows are all after it


class Refresher:
    """One refresh of the DB-derived gauges; keeps the webhook watermark between refreshes."""

    def __init__(self, factory: Factory, *, since: datetime | None = None) -> None:
        self._factory = factory
        self.after = since or datetime.now(UTC)
        self.after_id = _START_ID

    async def refresh(self) -> None:
        async with system_session(self._factory) as db:
            rows = (await db.execute(_AGGREGATES)).all()
            ages = await self._ages(db)
        oldest = apply_aggregates((r.metric, r.stream, r.state, float(r.total)) for r in rows)
        set_queue(dict(await queue.stats(self._factory)), oldest)
        for row in ages:
            record_webhook_age(float(row.age_seconds))
        if ages:
            self.after, self.after_id = ages[-1].received, ages[-1].event_id

    async def _ages(self, db: AsyncSession) -> Sequence[Row[Any]]:
        """The rows after the watermark, in (received, id) order, up to ``AGES_PAGES`` pages."""
        ages: list[Row[Any]] = []
        after, after_id = self.after, self.after_id
        for _ in range(AGES_PAGES):
            page = (await db.execute(_AGES, {"after": after, "after_id": after_id})).all()
            ages.extend(page)
            if len(page) < AGES_PAGE:
                break
            after, after_id = page[-1].received, page[-1].event_id
        return ages


async def _refresh_until(
    refresher: Refresher, engine: AsyncEngine, every: float, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            if queue_verified(engine):  # nothing is read before the queue check has passed
                await refresher.refresh()
        except Exception as exc:  # the next tick tries again; the claim loop is unaffected
            logger.warning("metrics refresh failed", extra={"error": type(exc).__name__})
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


@asynccontextmanager
async def metrics_running(
    settings: Settings, factory: Factory, stop: asyncio.Event, *, engine: AsyncEngine
) -> AsyncIterator[Refresher]:
    """The listener (when configured) and the refresh task, for as long as the block runs. A
    refused bind address raises ``BindRefused`` before anything starts. ``engine`` is the one
    ``verify_queue`` checked: each refresh waits for its check to have passed."""
    server = None
    if settings.metrics_bind is not None:
        server = await serve_metrics(settings.metrics_bind, settings.metrics_port)
    refresher = Refresher(factory)
    task = asyncio.ensure_future(
        _refresh_until(refresher, engine, settings.metrics_refresh_seconds, stop)
    )
    try:
        yield refresher
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        if server is not None:
            server.close()
            await server.wait_closed()
