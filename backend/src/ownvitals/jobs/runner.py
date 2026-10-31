"""From a claimed row to a settled run (ADR 0005 sections 2 to 6).

``execute`` issues the owner's context (``ownvitals.jobs.issuer``), reads the job and its input
in a fenced transaction as the owner, runs the kind's handler with a heartbeat and records how
the attempt ended, exactly once and fenced:

- the handler's ``Complete``: ``completed``, its ``store`` in the same transaction (``cancelled``
  instead when a cancel was requested meanwhile);
- ``RunStopped``: ``cancelled``, a hand-back on shutdown (the attempt not counted), or nothing
  when the lease is lost (the reaper owns the job then);
- a failure: retried with backoff or ``failed``; a ``JobFailure`` raised by ``store`` rolls the
  store back and counts the same way (any other exception there is ``internal``, a database blip
  ``upstream_unavailable``).

Only categories and exception type names are logged, never texts.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.db import DatabaseRoleError
from ownvitals.jobs import transitions
from ownvitals.jobs.errors import JobFailure, LeaseLost, RunStopped, backoff_seconds, failure_of
from ownvitals.jobs.fence import FencedSession, Lease, fenced_session
from ownvitals.jobs.issuer import NO_ACCOUNTS, Accounts, Claimed, issue, owner_context
from ownvitals.jobs.kinds import Complete, KindRegistry, Outcome
from ownvitals.jobs.run import JobRun

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]

_LOAD = text(
    "SELECT kind, input, attempt FROM jobs "
    "WHERE id = :job_id AND owner_id = :owner_id AND lease_version = :v"
)


@dataclass(frozen=True, slots=True)
class RunConfig:
    lease_seconds: int = 120
    accounts: Accounts = NO_ACCOUNTS


@dataclass(eq=False)
class Execution:
    """What the worker knows about one claimed job while it runs."""

    claimed: Claimed
    lease: Lease | None = None
    run: JobRun | None = None
    settled: bool = False  # its attempt was recorded, or it is not this worker's to record
    result: str | None = None  # the state written, or "lost"

    def needs_hand_back(self) -> bool:
        return not self.settled


def _log(execution: Execution, message: str, **extra: Any) -> None:
    logger.info(message, extra={"job_id": str(execution.claimed.job_id), **extra})


async def _record(execution: Execution, factory: Factory, write: Any) -> None:
    """Run ``write(fs)`` in a fenced transaction; a lost lease records nothing."""
    if execution.lease is None:
        raise RuntimeError("no lease to record on")
    try:
        async with fenced_session(factory, execution.lease) as fs:
            execution.result = await write(fs)
    except LeaseLost:
        execution.result = "lost"
    execution.settled = True


async def hand_back(execution: Execution, factory: Factory) -> None:
    """The shutdown hand-back (ADR 0005 section 5): fenced requeue, the attempt not counted.
    A run stopped before its context was issued is handed back as its bare owner (the requeue
    touches only the owner's job row)."""
    if execution.lease is None:
        claimed = execution.claimed
        execution.lease = Lease(
            claimed.job_id, claimed.lease_version, owner_context(claimed.owner_id)
        )
    await _record(execution, factory, transitions.hand_back)
    _log(execution, "job handed back", job_state=execution.result)


async def _finish(execution: Execution, factory: Factory, outcome: Outcome) -> None:
    async def write(fs: FencedSession) -> str:
        if fs.cancel_requested:
            return await transitions.cancel(fs)
        if outcome.store is not None:
            await outcome.store(fs)
        return await transitions.complete(fs)

    await _record(execution, factory, write)


async def _failed(execution: Execution, factory: Factory, run: JobRun, failure: JobFailure) -> None:
    category = failure.category
    delay = backoff_seconds(run.attempt, failure.retry_after)

    async def write(fs: FencedSession) -> str:
        return await transitions.retry(fs, category, delay)

    await _record(execution, factory, write)
    _log(execution, "job attempt failed", category=category, job_state=execution.result)


async def _pipeline(run: JobRun, kind_run: Any) -> Outcome:
    task: asyncio.Task[Outcome] = asyncio.ensure_future(kind_run(run))
    run.on_lost(task.cancel)  # a lost lease abandons the run (not a provider call in flight)
    heartbeat = asyncio.ensure_future(run.heartbeat())
    try:
        return await task
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def execute(
    execution: Execution,
    factory: Factory,
    registry: KindRegistry,
    config: RunConfig,
    shutdown: asyncio.Event,
) -> None:
    claimed = execution.claimed
    issued = await issue(factory, claimed, config.accounts)
    execution.lease = issued.lease
    if not issued.active:
        await _record(execution, factory, lambda fs: transitions.fail(fs, "account_disabled"))
        _log(execution, "job owner not active", job_state=execution.result)
        return
    try:
        async with fenced_session(factory, issued.lease) as fs:
            row = (await fs.db.execute(_LOAD, issued.lease.params())).one()
    except LeaseLost:
        execution.settled, execution.result = True, "lost"
        return
    kind = registry.get(row.kind)
    if kind is None:
        await _record(execution, factory, lambda fs: transitions.fail(fs, "internal"))
        return
    try:
        parsed = kind.parse_input(row.input)
    except ValidationError:
        await _record(execution, factory, lambda fs: transitions.fail(fs, "invalid_input"))
        return
    run = JobRun(
        factory,
        issued.lease,
        kind=kind.name,
        input=parsed,
        attempt=row.attempt,
        lease_seconds=config.lease_seconds,
        shutdown=shutdown,
    )
    execution.run = run
    _log(execution, "job started", kind=kind.name, attempt=row.attempt)
    try:
        outcome = await _pipeline(run, kind.run)
    except RunStopped as stopped:
        await _stopped(execution, factory, stopped.reason)
        return
    except LeaseLost:
        execution.settled, execution.result = True, "lost"
        return
    except DatabaseRoleError:
        raise  # not the job's failure: the worker stops and hands the lease back
    except asyncio.CancelledError:
        if run.fatal is not None:  # abandoned by the heartbeat's DatabaseRoleError
            raise run.fatal from None
        if run.lost and not shutdown.is_set():
            execution.settled, execution.result = True, "lost"
            return  # abandoned after the lease was lost; the reaper owns the job
        raise  # shutdown: the worker hands the lease back
    except Exception as exc:
        if run.fatal is not None:  # met by the heartbeat during a call that then failed
            raise run.fatal from None
        if run.lost:
            execution.settled, execution.result = True, "lost"
            return
        if not isinstance(exc, JobFailure):
            logger.warning("job handler raised", extra={"error_type": type(exc).__name__})
        await _failed(execution, factory, run, failure_of(exc))
        return
    if not isinstance(outcome, Complete):
        await _record(execution, factory, lambda fs: transitions.fail(fs, "internal"))
        return
    try:
        if run.fatal is not None:  # the heartbeat met it after the run's last checkpoint
            raise run.fatal
        await _finish(execution, factory, outcome)
    except JobFailure as failure:  # the kind's store refused (rolled back): its category
        await _failed(execution, factory, run, failure)
    except DatabaseRoleError:
        raise  # a drifted queue or a privileged role: the worker stops
    except Exception as exc:  # storing the result failed and was rolled back
        logger.warning("job store failed", extra={"error_type": type(exc).__name__})
        await _failed(execution, factory, run, failure_of(exc))
    _log(execution, "job attempt ended", job_state=execution.result)


async def _stopped(execution: Execution, factory: Factory, reason: str) -> None:
    if reason == "lost":
        execution.settled, execution.result = True, "lost"
    elif reason == "shutdown":
        await hand_back(execution, factory)
    else:
        await _record(execution, factory, transitions.cancel)
        _log(execution, "job cancelled", job_state=execution.result)
