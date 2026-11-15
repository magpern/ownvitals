"""The ``estimated`` recompute and its sweep (#191; ADR 0006 sections 1, 9 and 15):
the ``ecg_recompute`` job kind.

One job covers the whole owner; its input names a connection only because ``queue_enqueue``
needs one to find the owner. It is queued

* after every sync, backfill or fetch of the ``ecg`` stream (a new row, or a rewritten signal), and
* by the nightly reconciliation of every connection: **the sweep**, which after a module version
  bump (a new ``PIPELINE_VERSION``) recomputes every recording within a day. Until then the
  overlay already treats an ``estimated`` of other gate modules as absent, and an ``hrv``-only
  bump keeps the gate results (``ecg.overlay``).

A recording is **current** when its ``estimated`` has the running ``algo_version`` and its
``input_fingerprint`` is the row's (``signalid``, payload hash, sampling rate); every other one
is recomputed: the signal is read in one transaction, analysed outside it (``ecg.pipeline``,
pure) and written in another, **only if the row still has the payload it was computed from**
(the same ``signalid``, payload hash and sampling rate). A rewrite in between leaves it to the
recompute the rewrite queues; a purge or a disconnect in between leaves nothing to write. A row
kept with ``signal`` NULL gets the ``signal_missing`` estimate (no gate result), and so does a
stored frame that no longer decodes.

**Writing ``estimated`` bumps no revision and re-evaluates nothing** (section 15): the statement
touches that column only, and this job queues no evaluation. ``origin`` is ``backfill`` (no
notifications); the claim lane is the scheduler's, like ``derive_normalize``'s (``queue_enqueue``
maps only ``withings_backfill`` to the backfill lane; ``docs/dev/derived.md``, "Known gaps").
It runs as the owner under the fence, so RLS applies. Logs carry counts and versions only, never
a sample, a value or an id. Registered in the worker's registry only.
"""

import json
import logging
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

import zstandard
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ecg.pipeline import PIPELINE_VERSION, analyse, to_estimated
from ownvitals.ingest.codec import SignalRejected, decode_signal, input_fingerprint
from ownvitals.ingest.revisions import current
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.run import JobRun

logger = logging.getLogger(__name__)

KIND: Final = "ecg_recompute"
SINGLETON_KEY: Final = "ecg_recompute"

OpenTx = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_CANDIDATES = text(
    "SELECT id, signalid, payload_hash, sampling_hz, "
    "estimated->>'algo_version' AS algo_version, "
    "estimated->>'input_fingerprint' AS input_fingerprint "
    "FROM ecg_recordings WHERE owner_id = :o ORDER BY recorded_at, id"
)
_SIGNAL = text(
    "SELECT signalid, signal, payload_hash, sampling_hz FROM ecg_recordings "
    "WHERE owner_id = :o AND id = :id"
)
_WRITE = text(
    "UPDATE ecg_recordings SET estimated = CAST(:e AS jsonb) "
    "WHERE owner_id = :o AND id = :id AND signalid = :s "
    "AND payload_hash IS NOT DISTINCT FROM CAST(:h AS bytea) "
    "AND sampling_hz IS NOT DISTINCT FROM CAST(:r AS integer)"
)


class RecomputeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: UUID
    origin: Literal["backfill"]


@dataclass(frozen=True, slots=True)
class Outcome:
    """Counts only: rows already ``current``, ``recomputed``, ``moved`` (the row changed or
    went between the read and the write; left to the next run) and ``undecodable`` frames."""

    current: int = 0
    recomputed: int = 0
    moved: int = 0
    undecodable: int = 0


def _bytes(value: object) -> bytes | None:
    return None if value is None else bytes(value)  # pyright: ignore[reportArgumentType]


def is_current(
    algo_version: object,
    stored_fingerprint: object,
    *,
    signalid: int,
    digest: bytes | None,
    sampling_hz: int | None,
) -> bool:
    """Section 15: an ``estimated`` is up to date when its pipeline version is the running one
    and its fingerprint is the row's. Pure."""
    return algo_version == PIPELINE_VERSION and stored_fingerprint == input_fingerprint(
        signalid, digest, sampling_hz
    )


def compute(
    signalid: int, frame: bytes | None, digest: bytes | None, sampling_hz: int | None, revision: int
) -> tuple[dict[str, Any], bool]:
    """The ``estimated`` of one row, and whether its frame failed to decode. Pure but for the
    numeric work: the same payload and versions give the same record."""
    leads: list[list[int]] | None = None
    undecodable = False
    if frame is not None:
        try:
            leads = decode_signal(frame)
        except (SignalRejected, zstandard.ZstdError, struct.error, ValueError):
            undecodable = True
    result = analyse(leads, sampling_hz)
    record = to_estimated(
        result,
        input_fingerprint=input_fingerprint(signalid, digest, sampling_hz),
        input_revision=revision,
    )
    return record, undecodable


async def recompute_owner(
    open_tx: OpenTx, owner: UUID, *, between: Callable[[], Awaitable[None]] | None = None
) -> Outcome:
    """Recompute every recording of ``owner`` whose ``estimated`` is not current (see the module
    docstring). ``between`` runs after each recording's read; a job passes its checkpoint."""
    async with open_tx() as db:
        rows = (await db.execute(_CANDIDATES, {"o": owner})).all()
    stale = [
        r.id
        for r in rows
        if not is_current(
            r.algo_version,
            r.input_fingerprint,
            signalid=int(r.signalid),
            digest=_bytes(r.payload_hash),
            sampling_hz=r.sampling_hz,
        )
    ]
    recomputed = moved = undecodable = 0
    for recording in stale:
        async with open_tx() as db:
            row = (await db.execute(_SIGNAL, {"o": owner, "id": recording})).first()
            revision = await current(db, owner, "ecg")
        if row is None:
            moved += 1
            continue
        digest = _bytes(row.payload_hash)
        record, broken = compute(
            int(row.signalid), _bytes(row.signal), digest, row.sampling_hz, revision
        )
        undecodable += int(broken)
        if between is not None:
            await between()
        params = {
            "o": owner,
            "id": recording,
            "s": int(row.signalid),
            "h": digest,
            "r": row.sampling_hz,
            "e": json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False),
        }
        async with open_tx() as db:
            written = await db.execute(_WRITE, params)
        if int(getattr(written, "rowcount", 0) or 0):
            recomputed += 1
        else:
            moved += 1
    return Outcome(
        current=len(rows) - len(stale), recomputed=recomputed, moved=moved, undecodable=undecodable
    )


async def run_recompute(job: JobRun) -> Complete:
    if not isinstance(job.input, RecomputeInput):
        raise JobFailure("invalid_input")

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with job.fenced() as fs:
            yield fs.db

    outcome = await recompute_owner(open_tx, job.ctx.user_id, between=job.checkpoint)
    logger.info(
        "ecg estimates recomputed",
        extra={
            "algo_version": PIPELINE_VERSION,
            "current": outcome.current,
            "recomputed": outcome.recomputed,
            "moved": outcome.moved,
            "undecodable": outcome.undecodable,
        },
    )
    return Complete()


def register_recompute_kind(registry: KindRegistry) -> None:
    if registry.get(KIND) is None:
        registry.register(JobKind(KIND, RecomputeInput, run_recompute))


async def enqueue_recompute(factory: async_sessionmaker[AsyncSession], connection_id: UUID) -> None:
    """Queue a recompute for the connection's owner. Best-effort like ``enqueue_evaluate``: the
    caller's work has committed, so a database error is logged by its class name only and the
    next ``ecg`` sync or the nightly sweep queues it again."""
    from ownvitals.jobs.queue import enqueue

    try:
        await enqueue(
            factory,
            connection_id=connection_id,
            kind=KIND,
            origin="scheduler",
            payload=json.dumps({"connection_id": str(connection_id), "origin": "backfill"}),
            singleton_key=SINGLETON_KEY,
            max_attempts=3,
            target_secret_id=None,
        )
    except SQLAlchemyError as exc:
        logger.warning("ecg recompute not queued", extra={"error_type": type(exc).__name__})
