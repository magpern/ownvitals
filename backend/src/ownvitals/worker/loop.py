"""The worker loop (ADR 0005 section 5).

Up to ``worker_concurrency`` runs as asyncio tasks. When a slot is free the loop calls
``queue_claim``, woken by ``LISTEN jobs_queued`` (``ownvitals.worker.listen``) or a poll every
``worker_poll_seconds``; it claims nothing until the role and queue checks of a connection have
passed (``queue_verified``). Every ``worker_maintenance_seconds`` (30 s) it runs the reaper. The
hourly auth purge of ADR 0003 section 1 joins the loop with the accounts issue (#10); M1 adds the
scheduler's ticks and the owner-less upkeep (ADR 0005 section 8).

Several workers are safe: ``SKIP LOCKED`` gives each job to one claimer, and the fence makes a
lost lease harmless.

**Shutdown** (SIGTERM; compose gives 30 s): stop claiming; every run stops at its next checkpoint
(no new provider call) and hands its lease back; runs still busy after
``worker_shutdown_wait_seconds`` (25 s) are cancelled and their leases handed back too. Each
hand-back is one fenced requeue (``attempt - 1``, ``lease_version + 1``) under the role's 5 s
``lock_timeout``; what is not handed back is reaped when its lease expires. A maintenance step in
progress at SIGTERM is cancelled (one transaction, rolled back and run again later); a claim is
left to finish (5 s ``connect_timeout``, 30 s ``statement_timeout``), since a claim cancelled
after its commit would strand its job until the reaper.

**A drifted queue or a privileged role** (``DatabaseRoleError``, ``QueueDriftError``) stops the
worker wherever it is raised: the background check, a claim, a run's or a maintenance step's new
connection, a run's heartbeat. It is never a job's failure. The running jobs are then handed back
as on SIGTERM, and ``run`` raises it.
"""

import asyncio
import contextlib
import logging
import os
import re
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.config import Settings
from ownvitals.db import (
    DatabaseRoleError,
    create_engine,
    create_session_factory,
    verify_database_role,
)
from ownvitals.db.queue_check import install_queue_check, queue_verified, verify_queue
from ownvitals.jobs import KINDS, NO_ACCOUNTS, Accounts, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute, hand_back
from ownvitals.worker.listen import listen

logger = logging.getLogger(__name__)

CHECK_BACKOFF_MAX_SECONDS = 30.0


def worker_pool(settings: Settings) -> dict[str, Any]:
    """The worker engine's pool: per run one handler and one heartbeat connection, plus the
    ``LISTEN`` connection and one for claims, maintenance and the checks; an overflow of one per
    run covers a run that opens a second transaction while its first is open. With the cap of 16
    runs that is at most 50 connections."""
    return {
        "pool_size": 2 * settings.worker_concurrency + 2,
        "max_overflow": settings.worker_concurrency,
    }


def create_worker_engine(settings: Settings, **kwargs: Any) -> AsyncEngine:
    """The app role's engine with the queue check on every new connection (ADR 0005 section 3);
    the role check is ``create_engine``'s own."""
    engine = create_engine(settings, **{**worker_pool(settings), **kwargs})
    install_queue_check(engine)
    return engine


async def verify_startup(engine: AsyncEngine) -> None:
    """The role check (ADR 0002 section 5), then the queue check (ADR 0005 section 3)."""
    await verify_database_role(engine)
    await verify_queue(engine)


def worker_id() -> str:
    """``host:pid:random``, within ``queue_claim``'s ``^[A-Za-z0-9._:-]{1,100}$``."""
    host = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname())[:60] or "worker"
    return f"{host}:{os.getpid()}:{secrets.token_hex(4)}"


async def _quietly(name: str, step: Callable[[], Awaitable[int]]) -> None:
    try:
        count = await step()
    except DatabaseRoleError:
        raise
    except Exception as exc:
        logger.warning(
            "job maintenance failed", extra={"step": name, "error_type": type(exc).__name__}
        )
        return
    if count:
        logger.info("job maintenance", extra={"step": name, "count": count})


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        *,
        registry: KindRegistry = KINDS,
        accounts: Accounts = NO_ACCOUNTS,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.factory = create_session_factory(engine)
        self.registry = registry
        self.id = worker_id()
        self.config = RunConfig(lease_seconds=settings.job_lease_seconds, accounts=accounts)
        self.shutdown = asyncio.Event()  # set on SIGTERM: runs stop at their next checkpoint
        self.wake = asyncio.Event()
        self.running: dict[asyncio.Task[None], Execution] = {}
        self.stop = asyncio.Event()  # the caller's stop (SIGTERM), once ``run`` started
        self._verify: asyncio.Task[None] | None = None
        self._next_check = 0.0  # monotonic time of the next check attempt
        self._check_delay = settings.worker_poll_seconds
        self.fatal: DatabaseRoleError | None = None  # stops the loop
        self.stranded: list[Execution] = []  # ended by it, handed back with the rest
        self.passes = 0  # loop passes, for tests and diagnostics
        self.checks = 0  # check attempts started

    async def maintain(self) -> None:
        """Reap once (ADR 0005 section 3)."""
        await _quietly("reap", lambda: queue.reap(self.factory))

    def verified(self) -> bool:
        """The role and queue checks passed on a connection of this engine (ADR 0005 section 3).
        Until they have, a check runs in the background (so a database that is down never blocks
        the loop or its shutdown), at most one at a time and with backoff between attempts (from
        ``worker_poll_seconds``, doubling up to ``CHECK_BACKOFF_MAX_SECONDS``); a drift raises
        ``DatabaseRoleError`` and stops the worker."""
        if queue_verified(self.engine):
            return True
        check = self._verify
        if check is not None:
            if not check.done():
                return False
            self._verify = None
            error = None if check.cancelled() else check.exception()
            if isinstance(error, DatabaseRoleError):
                raise error
            if error is not None:
                logger.warning("queue check failed", extra={"error_type": type(error).__name__})
            if queue_verified(self.engine):
                return True
            self._next_check = time.monotonic() + self._check_delay
            self._check_delay = min(self._check_delay * 2, CHECK_BACKOFF_MAX_SECONDS)
            return False
        if time.monotonic() >= self._next_check:
            self.checks += 1
            self._verify = asyncio.ensure_future(verify_startup(self.engine))
        return False

    def _started(self, execution: Execution) -> None:
        task = asyncio.ensure_future(self._execute(execution))
        self.running[task] = execution
        task.add_done_callback(self._ended)

    def _ended(self, task: asyncio.Task[None]) -> None:
        self.running.pop(task, None)
        self.wake.set()

    async def _execute(self, execution: Execution) -> None:
        try:
            await execute(execution, self.factory, self.registry, self.config, self.shutdown)
        except asyncio.CancelledError:
            raise
        except DatabaseRoleError as exc:  # the loop stops and hands the lease back
            if self.fatal is None:
                self.fatal = exc
            self.stranded.append(execution)
            self.wake.set()
        except Exception as exc:  # left to the reaper when its lease expires
            logger.warning("job run failed", extra={"error_type": type(exc).__name__})

    async def claim_free_slots(self) -> int:
        claimed = 0
        kinds = self.registry.names()
        while (
            not self.shutdown.is_set()
            and not self.stop.is_set()
            and len(self.running) < self.settings.worker_concurrency
        ):
            row = await queue.claim(self.factory, self.id, self.settings.job_lease_seconds, kinds)
            if row is None:
                break
            claimed += 1
            self._started(Execution(row))
        return claimed

    async def _claim_step(self) -> None:
        try:
            if self.verified():
                await self.claim_free_slots()
        except asyncio.CancelledError:
            raise
        except DatabaseRoleError:
            raise  # a drifted queue or a privileged role: no override, the worker stops
        except Exception as exc:
            logger.warning("job claim failed", extra={"error_type": type(exc).__name__})

    async def run(self, stop: asyncio.Event) -> None:
        self.stop = stop
        listen_stop = asyncio.Event()
        listener = asyncio.ensure_future(listen(self.engine, self.wake, listen_stop))
        stopper = asyncio.ensure_future(self._wake_on(stop))
        next_maintenance = time.monotonic()
        try:
            while not stop.is_set():
                self.passes += 1
                if self.fatal is not None:
                    raise self.fatal
                await self._claim_step()
                timeout = self.settings.worker_poll_seconds
                if queue_verified(self.engine) and not stop.is_set():
                    # Maintenance runs only on a verified queue, so its timer never shortens the
                    # wait while the database is down (no hot loop).
                    if time.monotonic() >= next_maintenance:
                        await self._unless_stopped(self.maintain(), stop)
                        next_maintenance = (
                            time.monotonic() + self.settings.worker_maintenance_seconds
                        )
                    timeout = min(timeout, max(next_maintenance - time.monotonic(), 0.01))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), timeout=timeout)
                self.wake.clear()
        finally:
            listen_stop.set()
            background = [t for t in (listener, stopper, self._verify) if t is not None]
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await self.stop_runs()

    @staticmethod
    async def _unless_stopped(step: Awaitable[None], stop: asyncio.Event) -> None:
        """Run ``step``; cancel it when ``stop`` is set first (shutdown is bounded)."""
        task = asyncio.ensure_future(step)
        stopped = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({task, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (task, stopped):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stopped, return_exceptions=True)
        error = None if task.cancelled() else task.exception()
        if error is not None:
            raise error

    async def _wake_on(self, stop: asyncio.Event) -> None:
        await stop.wait()
        self.shutdown.set()  # at once: runs stop at their next checkpoint, nothing more is claimed
        self.wake.set()

    async def stop_runs(self) -> None:
        """The shutdown hand-back of ADR 0005 section 5."""
        self.shutdown.set()
        tasks = list(self.running)
        executions = [self.running[t] for t in tasks]
        if tasks:
            _, busy = await asyncio.wait(tasks, timeout=self.settings.worker_shutdown_wait_seconds)
            for task in busy:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        # A run that raised ``DatabaseRoleError`` while this waited for it is both stranded and
        # one of ``executions``: hand it back once (``Execution`` hashes by identity).
        everyone = dict.fromkeys([*self.stranded, *executions])
        pending = [e for e in everyone if e.needs_hand_back()]
        self.stranded = []
        results = await asyncio.gather(
            *(hand_back(e, self.factory) for e in pending), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("job hand-back failed", extra={"error_type": type(result).__name__})


async def run_worker(
    settings: Settings,
    stop: asyncio.Event,
    *,
    registry: KindRegistry = KINDS,
    accounts: Accounts = NO_ACCOUNTS,
) -> None:
    """Run until ``stop`` is set, then hand back what is still running."""
    engine = create_worker_engine(settings)  # connects lazily
    try:
        logger.info("worker started")
        await Worker(settings, engine, registry=registry, accounts=accounts).run(stop)
        logger.info("worker stopped")
    finally:
        await engine.dispose()
