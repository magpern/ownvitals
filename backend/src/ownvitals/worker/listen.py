"""``LISTEN jobs_queued`` on a connection of its own (ADR 0005 section 5).

A notification only wakes the claim loop; it carries nothing (the payload is empty) and the
connection never queries a table. If the listener is down, the worker still polls every
``worker_poll_seconds``, and the listener reconnects with backoff: a lost notification only adds
latency.
"""

import asyncio
import contextlib
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

CHANNEL = "jobs_queued"
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
NOTIFY_TIMEOUT_SECONDS = 5.0


async def _listen_once(engine: AsyncEngine, wake: asyncio.Event, stop: asyncio.Event) -> None:
    async with engine.connect() as connection:
        raw = await connection.get_raw_connection()
        driver: Any = raw.driver_connection  # psycopg.AsyncConnection
        try:
            await driver.set_autocommit(True)
            await driver.execute(f"LISTEN {CHANNEL}")
            logger.debug("listening for queued jobs")
            while not stop.is_set():
                async for _ in driver.notifies(timeout=NOTIFY_TIMEOUT_SECONDS):
                    wake.set()
                    if stop.is_set():
                        break
        finally:
            # Never hand this connection (autocommit, LISTEN) back to the pool.
            await connection.invalidate()


async def listen(engine: AsyncEngine, wake: asyncio.Event, stop: asyncio.Event) -> None:
    """Until ``stop`` is set (or cancelled): set ``wake`` on every ``jobs_queued``."""
    delay = RECONNECT_MIN_SECONDS
    while not stop.is_set():
        try:
            await _listen_once(engine, wake, stop)
            delay = RECONNECT_MIN_SECONDS
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the database is down: poll, and try again later
            logger.info("job listener unavailable", extra={"error_type": type(exc).__name__})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)
