"""The ``derive_daily`` job kind (#186): the worker pass that fills ``metric_daily``.

One job covers the whole owner (every connection's observations); its input names a connection
only because ``queue_enqueue`` needs one to find the owner. ``origin`` keeps live and backfill
work apart (PLAN §7.1 step 7): the two have separate singleton keys (``daily_live``,
``daily_backfill``), so a backfill pass never collapses into, or delays, a live one. The claim
lane of both is the scheduler's (``queue_enqueue`` maps only ``withings_backfill`` to the
backfill lane; see ``docs/dev/derived.md``, "Known gaps").

The job is enqueued by ``derive_normalize`` after its pass (``run_normalize``), with the same
origin, because the daily values read what that pass wrote. After a pass that ended ``written``
or ``current`` it queues the rule evaluation (``derive_evaluate``, #187) with its own origin; a
``stale``, ``behind`` or ``nothing`` pass queues nothing (whatever moved the inputs queues a
newer pass). It runs as the owner under the fence, so RLS applies. Registered in the worker's
registry only.
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

from ownvitals.derived.daily_db import daily_owner
from ownvitals.derived.evaluate_job import enqueue_evaluate
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun

logger = logging.getLogger(__name__)

KIND: Final = "derive_daily"
Origin = Literal["live", "backfill"]


class DailyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: UUID
    origin: Origin


def singleton_key(origin: str) -> str:
    """One queued pass per owner and origin (the queue scopes the key to the owner)."""
    return f"daily_{origin}"


async def run_daily(job: JobRun) -> Complete:
    parsed = job.input
    if not isinstance(parsed, DailyInput):
        raise JobFailure("invalid_input")

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with job.fenced() as fs:
            yield fs.db

    outcome = await daily_owner(open_tx, job.ctx.user_id, between=job.checkpoint)
    logger.info(
        "daily values computed",
        extra={
            "origin": parsed.origin,
            "status": outcome.status,
            "inserted": outcome.inserted,
            "updated": outcome.updated,
            "deleted": outcome.deleted,
            "skipped": outcome.skipped,
        },
    )
    if outcome.status in ("written", "current"):
        await enqueue_evaluate(job.factory, parsed.connection_id, parsed.origin)
    return Complete()


def register_daily_kind(registry: KindRegistry) -> None:
    if registry.get(KIND) is None:
        registry.register(JobKind(KIND, DailyInput, run_daily))


async def enqueue_daily(
    factory: async_sessionmaker[AsyncSession], connection_id: UUID, origin: Origin
) -> None:
    """Queue a pass for the connection's owner, after the normaliser pass. Best-effort like
    ``enqueue_normalize``: that pass has committed, so a database error is logged by its class
    name only and the next normaliser pass queues this one again."""
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
            "daily values not queued", extra={"origin": origin, "error_type": type(exc).__name__}
        )
