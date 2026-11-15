"""The ``derive_scores`` job kind (#188, ADR 0012): the worker pass that writes the day's scores.

One job covers the whole owner; its input names a connection only because ``queue_enqueue``
needs one to find the owner. ``derive_evaluate`` queues it after a pass that ended ``written`` or
``current``, so the nightly reconciliation also recomputes the day's scores. It runs as the owner
under the fence, so RLS applies. Registered in the worker's registry only.
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
from ownvitals.config_scores import ScoreSettings
from ownvitals.derived.scores_store import score_owner
from ownvitals.derived.scores_tables import load_table
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun

logger = logging.getLogger(__name__)

KIND: Final = "derive_scores"
SINGLETON_KEY: Final = "scores"


class ScoresInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: UUID


async def run_scores(job: JobRun) -> Complete:
    if not isinstance(job.input, ScoresInput):
        raise JobFailure("invalid_input")

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with job.fenced() as fs:
            yield fs.db

    table = load_table(ScoreSettings().his_band_table)
    outcome = await score_owner(
        open_tx, job.ctx.user_id, settings=Settings(), table=table, between=job.checkpoint
    )
    logger.info("scores computed", extra={"status": outcome.status, "changed": outcome.changed})
    return Complete()


def register_scores_kind(registry: KindRegistry) -> None:
    if registry.get(KIND) is None:
        registry.register(JobKind(KIND, ScoresInput, run_scores))


async def enqueue_scores(factory: async_sessionmaker[AsyncSession], connection_id: UUID) -> None:
    """Queue a pass for the connection's owner. Best-effort like ``enqueue_evaluate``: the
    pass before it has committed, so a database error is logged by its class name only and the
    next evaluation queues this one again."""
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
        logger.warning("scores not queued", extra={"error_type": type(exc).__name__})
