"""One run of a claimed job: its checkpoints, fenced writes, provider calls and heartbeat.

A kind's handler gets a ``JobRun`` and reaches the database only through it:

- ``checkpoint()`` between pages and windows of a sync, and wherever the kind may stop (ADR 0005
  section 6). It raises ``RunStopped`` when the lease is gone (``lost``), the worker is shutting
  down (``shutdown``) or the user asked to cancel (``cancelled``; the fence returns
  ``cancel_requested_at``).
- ``progress(done, total)``: a checkpoint that also appends a ``progress`` event.
- ``fenced()``: a fenced transaction for the kind's own writes. ``LeaseLost`` marks the run lost.
- ``provider_call(deadline)``: around one call to a provider (from M1, Withings through
  ``ownvitals.egress``). It starts with a checkpoint whose fenced transaction also extends the
  lease to at least ``now()`` + the call's total deadline + 30 s, and commits before the call: **no
  transaction is open across a provider call**. So no call starts after a cancel, a lost lease or
  SIGTERM; while it is open the call counts as in flight: a cancel does not interrupt it, a lost
  lease does not abandon it, and shutdown waits for it.

**For kind authors: never nest them.** No ``checkpoint()``, ``progress()``, ``provider_call()`` or
second ``fenced()`` inside an open ``fenced()`` block: each opens its own transaction, whose fence
waits for the row lock the outer one holds, and fails after the role's 5 s ``lock_timeout``. Do
the fenced write, leave the block, then checkpoint.

The heartbeat extends the lease every lease/3 with ``GREATEST`` (it never shortens an extension
made for a call) in its own short fenced transaction. Lost (0 rows, or 3 database errors in a
row), it marks the run lost and abandons whatever is not a provider call in flight. A
``DatabaseRoleError`` there (a drifted queue or a privileged role) is not a blip: it is kept as
``fatal`` and abandons the run the same way; the next checkpoint raises it, and the runner raises
it in place of the run's result, so the worker stops and hands the lease back.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Final

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext
from ownvitals.db import DatabaseRoleError
from ownvitals.jobs.errors import LeaseLost, RunStopped
from ownvitals.jobs.fence import FencedSession, Lease, fenced_session

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]
HEARTBEAT_ERRORS: Final = 3


class JobRun:
    def __init__(
        self,
        factory: Factory,
        lease: Lease,
        *,
        kind: str,
        input: BaseModel,
        attempt: int,
        lease_seconds: float,
        shutdown: asyncio.Event,
    ) -> None:
        self.factory = factory
        self.lease = lease
        self.kind = kind
        self.input = input
        self.attempt = attempt
        self.lease_seconds = lease_seconds
        self.shutdown = shutdown
        self.lost = False
        self.cancel_requested = False
        self.in_flight = 0
        self.fatal: DatabaseRoleError | None = None  # met by the heartbeat: the worker stops
        self._on_lost: Callable[[], object] | None = None

    @property
    def ctx(self) -> UserContext:
        return self.lease.ctx

    def mark_lost(self) -> None:
        if not self.lost:
            self.lost = True
            logger.warning("job lease lost", extra={"job_id": str(self.lease.job_id)})
            if self._on_lost is not None and self.in_flight == 0:
                self._on_lost()

    def mark_fatal(self, error: DatabaseRoleError) -> None:
        """The heartbeat met a drifted queue or a privileged role: abandon the run as for a lost
        lease (not a provider call in flight); ``fatal`` stops the worker."""
        if self.fatal is None:
            self.fatal = error
            if self._on_lost is not None and self.in_flight == 0:
                self._on_lost()

    def on_lost(self, callback: Callable[[], object]) -> None:
        self._on_lost = callback

    @asynccontextmanager
    async def fenced(self) -> AsyncIterator[FencedSession]:
        if self.lost:
            raise LeaseLost
        try:
            async with fenced_session(self.factory, self.lease) as fs:
                yield fs
                if fs.cancel_requested:
                    self.cancel_requested = True
        except LeaseLost:
            self.mark_lost()
            raise

    def _may_go_on(self) -> None:
        if self.fatal is not None:
            raise self.fatal
        if self.lost:
            raise RunStopped("lost")
        if self.shutdown.is_set():
            raise RunStopped("shutdown")

    def _stop_if_cancelled(self, cancel: bool) -> None:
        if cancel:
            self.cancel_requested = True
            raise RunStopped("cancelled")

    async def checkpoint(self) -> None:
        self._may_go_on()
        try:
            async with self.fenced() as fs:
                cancel = fs.cancel_requested
        except LeaseLost:
            raise RunStopped("lost") from None
        self._stop_if_cancelled(cancel)

    async def progress(self, done: int, total: int) -> None:
        """A checkpoint that records ``progress {done, total}`` (counts only, never values)."""
        self._may_go_on()
        try:
            async with self.fenced() as fs:
                await fs.event("progress", {"done": done, "total": total})
                cancel = fs.cancel_requested
        except LeaseLost:
            raise RunStopped("lost") from None
        self._stop_if_cancelled(cancel)

    @asynccontextmanager
    async def provider_call(self, deadline_seconds: float) -> AsyncIterator[None]:
        """Around one provider call whose total deadline is ``deadline_seconds``."""
        if not deadline_seconds > 0:
            raise ValueError("a provider call needs a positive deadline")
        self._may_go_on()
        try:
            async with self.fenced() as fs:
                cancel = await fs.extend_lease_for_call(deadline_seconds)
        except LeaseLost:
            raise RunStopped("lost") from None
        self._stop_if_cancelled(cancel)
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1
            stopped = self.lost or self.fatal is not None
            if stopped and self.in_flight == 0 and self._on_lost is not None:
                self._on_lost()

    async def heartbeat(self) -> None:
        """Until cancelled: extend the lease every lease/3; stop when it is lost."""
        interval = self.lease_seconds / 3
        errors = 0
        while not self.lost:
            await asyncio.sleep(interval)
            try:
                async with fenced_session(self.factory, self.lease) as fs:
                    cancel = await fs.extend_lease(self.lease_seconds, heartbeat=True)
            except LeaseLost:
                self.mark_lost()
                return
            except DatabaseRoleError as exc:  # not a blip: the worker stops
                self.mark_fatal(exc)
                return
            except Exception as exc:  # the database blinked; three in a row lose the run
                errors += 1
                logger.warning("job heartbeat failed", extra={"error_type": type(exc).__name__})
                if errors >= HEARTBEAT_ERRORS:
                    self.mark_lost()
                    return
                continue
            errors = 0
            if cancel:
                self.cancel_requested = True
