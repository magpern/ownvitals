"""The ``derive_evaluate`` job kind (#187): the worker pass that evaluates the v1 rules.

One job covers the whole owner; its input names a connection only because ``queue_enqueue``
needs one to find the owner. ``origin`` keeps live and backfill work apart in the queue
(``evaluate_live``, ``evaluate_backfill``); a finding's own origin is computed per episode
(ADR 0010 §4), never taken from the job.

``derive_daily`` queues it after a pass that ended ``written`` or ``current``, with its own
origin, so the nightly reconciliation (normaliser, daily values, evaluation) also re-evaluates
Q1's time-dependent part every day. It runs as the owner under the fence, so RLS applies.
Registered in the worker's registry only.
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.derived.findings_store import evaluate_owner
from ownvitals.derived.scores_job import enqueue_scores
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun

logger = logging.getLogger(__name__)

KIND: Final = "derive_evaluate"
Origin = Literal["live", "backfill"]


class EvaluateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: UUID
    origin: Origin


def singleton_key(origin: str) -> str:
    """One queued pass per owner and origin (the queue scopes the key to the owner)."""
    return f"evaluate_{origin}"


async def run_evaluate(job: JobRun) -> Complete:
    parsed = job.input
    if not isinstance(parsed, EvaluateInput):
        raise JobFailure("invalid_input")

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with job.fenced() as fs:
            yield fs.db

    outcome = await evaluate_owner(
        open_tx, job.ctx.user_id, settings=Settings(), between=job.checkpoint
    )
    logger.info(
        "rules evaluated",
        extra={
            "origin": parsed.origin,
            "status": outcome.status,
            "inserted": outcome.inserted,
            "updated": outcome.updated,
            "ended": outcome.ended,
            "deleted": outcome.deleted,
            "pending": outcome.pending,
        },
    )
    if outcome.status in ("written", "current"):
        await enqueue_scores(job.factory, parsed.connection_id)
    return Complete()


def register_evaluate_kind(registry: KindRegistry) -> None:
    if registry.get(KIND) is None:
        registry.register(JobKind(KIND, EvaluateInput, run_evaluate))


async def enqueue_evaluate(
    factory: async_sessionmaker[AsyncSession], connection_id: UUID, origin: Origin
) -> None:
    """Queue a pass for the connection's owner, after the daily pass. Best-effort like
    ``enqueue_daily``: that pass has committed, so a database error is logged by its class name
    only and the next daily pass queues this one again."""
    from ownvitals.jobs.queue import enqueue

    try:
        await enqueue(
            factory,
            connection_id=connection_id,
            kind=KIND,
            origin="scheduler",
            payload=json.dumps({"connection_id": str(connection_id), "origin": origin}),
            singleton_key=singleton_key(origin),
            max_attempts=3,
            target_secret_id=None,
        )
    except SQLAlchemyError as exc:
        logger.warning(
            "evaluation not queued", extra={"origin": origin, "error_type": type(exc).__name__}
        )
