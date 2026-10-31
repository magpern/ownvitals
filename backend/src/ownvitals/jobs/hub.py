"""The web process's wake-up hub for job event streams (ADR 0005 section 7).

One dedicated ``LISTEN job_events`` connection per web process. It never queries a table: a
notification carries a job id and nothing else, and the hub only sets the ``asyncio.Event`` of the
streams subscribed to that id. The stream then reads in a user session, so a notification for
someone else's job reveals nothing: a wake-up carries no data, and a stream shows only what its
own owner-scoped read returns. (A stream subscribes just before its first read; the route
has already answered 404 for a job that is not the caller's.)

If the listener is down, ``listening`` is false and streams poll every few seconds; the listener
reconnects with backoff, and every subscriber is woken when it goes down and when it is back (a
notification may have been missed in between). A lost notification only adds latency.

The listener holds one connection of the web engine's pool for the life of the process: of the
default ``pool_size`` 5 plus ``max_overflow`` 10, requests share the other 14. Each stream holds
none between its short reads.

**Shutdown**: ``close()`` (``close_threadsafe()`` from uvicorn's signal handler, see
``ownvitals.__main__``) sets ``closing`` and wakes every stream, which then ends; the browser
reconnects to the next process with its last id. Without it an open stream (up to 10 minutes)
would hold uvicorn's graceful shutdown, and the lifespan's ``stop()`` and engine disposal would
never run.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.jobs.events import CHANNEL

logger = logging.getLogger(__name__)

RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
NOTIFY_TIMEOUT_SECONDS = 5.0


class Subscription:
    """The wake-up of one stream: ``wake`` is set by a notification for its job."""

    def __init__(self) -> None:
        self.wake = asyncio.Event()

    async def wait(self, timeout: float) -> None:  # noqa: ASYNC109 (a wait, not a deadline)
        """Until woken or ``timeout`` seconds; clears the wake-up so the next read starts clean."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.wake.wait(), timeout)
        self.wake.clear()


class EventHub:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._subscriptions: dict[UUID, set[Subscription]] = {}
        self._task: asyncio.Task[None] | None = None
        self.listening = False
        self.closing = False
        self._delay = RECONNECT_MIN_SECONDS
        self._loop: asyncio.AbstractEventLoop | None = None

    @contextmanager
    def subscribe(self, job_id: UUID) -> Iterator[Subscription]:
        subscription = Subscription()
        self._subscriptions.setdefault(job_id, set()).add(subscription)
        try:
            yield subscription
        finally:
            members = self._subscriptions.get(job_id)
            if members is not None:
                members.discard(subscription)
                if not members:
                    del self._subscriptions[job_id]

    def notify(self, payload: str) -> None:
        """A notification: the payload is a job id; anything else is ignored."""
        try:
            job_id = UUID(payload)
        except ValueError:
            return
        for subscription in self._subscriptions.get(job_id, ()):
            subscription.wake.set()

    def wake_all(self) -> None:
        for members in self._subscriptions.values():
            for subscription in members:
                subscription.wake.set()

    def _set_listening(self, value: bool) -> None:
        if self.listening != value:
            self.listening = value
            self.wake_all()

    async def _listen_once(self) -> None:
        async with self._engine.connect() as connection:
            raw = await connection.get_raw_connection()
            driver: Any = raw.driver_connection  # psycopg.AsyncConnection
            try:
                await driver.set_autocommit(True)
                await driver.execute(f"LISTEN {CHANNEL}")
                self._set_listening(True)
                self._delay = RECONNECT_MIN_SECONDS  # a good connection resets the backoff
                while True:
                    async for notification in driver.notifies(timeout=NOTIFY_TIMEOUT_SECONDS):
                        self.notify(notification.payload)
            finally:
                self._set_listening(False)
                # Never hand this connection (autocommit, LISTEN) back to the pool.
                await connection.invalidate()

    async def run(self) -> None:
        """Listen until cancelled, reconnecting with backoff."""
        while True:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the database is down: streams poll meanwhile
                logger.info(
                    "job event listener unavailable", extra={"error_type": type(exc).__name__}
                )
            await asyncio.sleep(self._delay)
            self._delay = min(self._delay * 2, RECONNECT_MAX_SECONDS)

    def close(self) -> None:
        """Shutdown has begun: every stream ends at its next wake-up, which is now."""
        self.closing = True
        self.wake_all()

    def close_threadsafe(self) -> None:
        """``close`` from outside the hub's event loop (a signal handler); a no-op before
        ``start``."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.close)

    def start(self) -> None:
        if self._task is None:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        self.close()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
