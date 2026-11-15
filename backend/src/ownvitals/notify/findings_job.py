"""The ``notify_findings`` job kind (#195): the worker's pass of ``ownvitals.notify.findings``.

One job covers the whole owner; its input names a connection only because ``queue_enqueue``
needs one to find the owner. It runs as the owner under the fence, so RLS applies, in one fenced
transaction. ``then_notify`` wraps the rule evaluation (``derive_evaluate``) and the
``estimated`` recompute (``ecg_recompute``) in the worker's registry (``worker.kinds``) so a
pass is queued after each successful attempt of either: a new live finding, a new link or a new
``estimated`` is decided at once, and the 6-hourly poll's chain (sync, normalise, daily,
evaluate) and the nightly reconciliation re-check the holds, which end at their bound at the
next pass. Registered in the worker's registry only. Logs carry counts only.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.config_notify import NotifySettings
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.kinds import Outcome
from ownvitals.jobs.run import JobRun
from ownvitals.notify.findings import notify_owner

logger = logging.getLogger(__name__)

KIND: Final = "notify_findings"
SINGLETON_KEY: Final = "notify_findings"


class NotifyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: UUID


async def run_notify(job: JobRun) -> Complete:
    if not isinstance(job.input, NotifyInput):
        raise JobFailure("invalid_input")

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with job.fenced() as fs:
            yield fs.db

    await job.checkpoint()
    counts = await notify_owner(
        open_tx, job.ctx.user_id, settings=Settings(), bound=NotifySettings().hold_bound
    )
    logger.info(
        "notifications decided",
        extra={
            "sent": counts.sent,
            "dropped": counts.dropped,
            "held": counts.held,
            "ended": counts.ended,
            "withdrawn": counts.withdrawn,
        },
    )
    return Complete()


def register_notify_kind(registry: KindRegistry) -> None:
    if registry.get(KIND) is None:
        registry.register(JobKind(KIND, NotifyInput, run_notify))


async def enqueue_notify(factory: async_sessionmaker[AsyncSession], connection_id: UUID) -> None:
    """Queue a pass for the connection's owner. Best-effort like ``enqueue_evaluate``: the
    caller's work has committed, so a database error is logged by its class name only and the
    next evaluation or recompute queues it again."""
    from ownvitals.jobs.queue import enqueue

    try:
        await enqueue(
            factory,
            connection_id=connection_id,
            kind=KIND,
            origin="scheduler",
            payload=json.dumps({"connection_id": str(connection_id)}),
            singleton_key=SINGLETON_KEY,
            max_attempts=3,
            target_secret_id=None,
        )
    except SQLAlchemyError as exc:
        logger.warning("notification pass not queued", extra={"error_type": type(exc).__name__})


def then_notify(kind: JobKind) -> JobKind:
    """``kind`` with a notification pass queued after each successful attempt. Its input must
    carry the ``connection_id`` the pass is queued for (the evaluation's and the recompute's
    do); a kind whose input has none queues nothing."""

    async def run(job: JobRun) -> Outcome:
        outcome = await kind.run(job)
        connection = getattr(job.input, "connection_id", None)
        if isinstance(connection, UUID):
            await enqueue_notify(job.factory, connection)
        return outcome

    return JobKind(kind.name, kind.input_model, run)


__all__ = [
    "KIND",
    "SINGLETON_KEY",
    "NotifyInput",
    "enqueue_notify",
    "register_notify_kind",
    "run_notify",
    "then_notify",
]
