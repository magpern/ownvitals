"""The ``derive_normalize`` job kind (#185, #229): the worker pass that fills ``observations``.

One job normalises the whole owner (every connection's measure groups, sleep summaries, activity
days and workouts: every source of ``normalize_db.SOURCES``, one pass each); its input names a
connection only because ``queue_enqueue`` needs one to find the owner. ``origin`` keeps live
and backfill work apart (PLAN §7.1 step 7): the two have separate singleton keys, so a backfill
pass never collapses into, or delays, a live one. The claim lane of both is the scheduler's
(``queue_enqueue`` maps only ``withings_backfill`` to the backfill lane; see
``docs/dev/derived.md``, "Known gaps").

The job is enqueued after an ingest commit (``enqueue_normalize``): the end of a backfill or
sync of a stream in ``TRIGGERS``, and of the nightly reconciliation. The enqueue runs through the
queue's definer function, with the job's own connection id; the pass itself runs as the owner
under the fence, so RLS holds. The enqueue is best-effort: the ingest has committed already, so a
refused enqueue (the connection was disconnected while the job ran) or a lost database
connection is logged and the job still completes; the next sync or the nightly reconciliation
queues the pass again.
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

from ownvitals.derived.normalize_db import SOURCES, normalise_all
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun

logger = logging.getLogger(__name__)

KIND: Final = "derive_normalize"
Origin = Literal["live", "backfill"]
# The ingest streams (``withings.streams.STREAMS`` names) whose backfill or sync queues a pass:
# the ``data_revisions`` streams of the sources.
TRIGGERS: Final = frozenset(source.stream for source in SOURCES)


class NormalizeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: UUID
    origin: Origin


def singleton_key(origin: str) -> str:
    """One queued pass per owner and origin (the queue scopes the key to the owner)."""
    return f"normalize_{origin}"


async def run_normalize(job: JobRun) -> Complete:
    parsed = job.input
    if not isinstance(parsed, NormalizeInput):
        raise JobFailure("invalid_input")

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with job.fenced() as fs:
            yield fs.db

    outcomes = await normalise_all(open_tx, job.ctx.user_id, between=job.checkpoint)
    for stream, outcome in outcomes.items():
        logger.info(
            "observations normalised",
            extra={
                "origin": parsed.origin,
                "stream": stream,
                "status": outcome.status,
                "inserted": outcome.inserted,
                "updated": outcome.updated,
                "deleted": outcome.deleted,
                "skipped": outcome.skipped,
            },
        )
    return Complete()


def register_normalize_kind(registry: KindRegistry) -> None:
    if registry.get(KIND) is None:
        registry.register(JobKind(KIND, NormalizeInput, run_normalize))


async def enqueue_normalize(
    factory: async_sessionmaker[AsyncSession], connection_id: UUID, origin: Origin
) -> None:
    """Queue a pass for the connection's owner, after an ingest commit. Best-effort (see the
    module docstring): a database error is logged by its class name only, never raised."""
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
            "normalisation not queued", extra={"origin": origin, "error_type": type(exc).__name__}
        )
