"""The database side of the scores pass (#188, ADR 0012): read, compute, verify, write.

``score_owner`` is one owner's pass, in two transactions opened by ``open_tx`` (a job passes its
fenced transaction, a test a user session). Every statement filters ``owner_id`` itself as well
as running under forced RLS.

1. **Read** (``scores_gather.gather``) the stream revisions first, then the inputs.
2. **Compute** (``scores_compute``, pure): the day's score of each kind.
3. **Write**, in one transaction: take the owner's normalisation lock (namespace 6, ADR 0005 §9),
   lock the revision rows ``FOR SHARE`` (an ingest bump or the purge of imported data waits), and
   **gather the inputs again**: the revisions and the fingerprint must be the ones read, else
   nothing is written (``stale``). The purge-race pattern of ``daily_db`` and ``findings_store``.

One row per ``(owner_id, kind, day)``: an upsert, so a recompute of the same data gives the same
row and an earlier day stays as history. ``input_revision`` is the sum of the counters of the
streams read. When the owner-only switch is off the pass writes no ``his`` row and deletes the
stored ones, like the experimental findings.
"""

import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.config import Settings
from ownvitals.db.queue_locks import NORMALISATION
from ownvitals.derived.scores_compute import compute_scores
from ownvitals.derived.scores_gather import Gathered, gather, read_revisions
from ownvitals.derived.scores_tables import TableState

OpenTx = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_LOCK = text("SELECT pg_catalog.pg_advisory_xact_lock(:ns, pg_catalog.hashtext(CAST(:o AS text)))")
_UPSERT = text(
    "INSERT INTO scores (owner_id, kind, day, value, components, algo_version, input_revision, "
    "input_fingerprint) VALUES (:o, :kind, :day, :value, CAST(:components AS jsonb), :a, :r, :f) "
    "ON CONFLICT (owner_id, kind, day) DO UPDATE SET value = EXCLUDED.value, "
    "components = EXCLUDED.components, algo_version = EXCLUDED.algo_version, "
    "input_revision = EXCLUDED.input_revision, input_fingerprint = EXCLUDED.input_fingerprint "
    "WHERE (scores.value, scores.components, scores.algo_version, scores.input_revision, "
    "scores.input_fingerprint) IS DISTINCT FROM (EXCLUDED.value, EXCLUDED.components, "
    "EXCLUDED.algo_version, EXCLUDED.input_revision, EXCLUDED.input_fingerprint)"
)
_DROP_HIS = text("DELETE FROM scores WHERE owner_id = :o AND kind = 'his'")


@dataclass(frozen=True, slots=True)
class Outcome:
    """``written`` (a row changed), ``current`` (the stored rows were already these), ``stale``
    (the inputs moved between read and write; nothing written), ``behind`` or ``nothing``."""

    status: str
    changed: int = 0


async def _write(
    db: AsyncSession,
    owner: UUID,
    read: Gathered,
    *,
    settings: Settings,
    table: TableState,
    now: datetime,
) -> Outcome:
    await db.execute(_LOCK, {"ns": NORMALISATION, "o": owner})
    if await read_revisions(db, owner, lock=True) != read.revisions:
        return Outcome("stale")
    again = await gather(db, owner, settings=settings, table=table, now=now)
    if not isinstance(again, Gathered) or again.fingerprint != read.fingerprint:
        return Outcome("stale")
    changed = 0
    if not read.inputs.experimental:
        changed += int(getattr(await db.execute(_DROP_HIS, {"o": owner}), "rowcount", 0) or 0)
    for kind, res in compute_scores(read.inputs).items():
        done = await db.execute(
            _UPSERT,
            {
                "o": owner,
                "kind": kind,
                "day": read.inputs.day,
                "value": None if res.value is None else str(res.value),
                "components": json.dumps(res.components, sort_keys=True),
                "a": res.algo_version,
                "r": read.input_revision,
                "f": read.fingerprint,
            },
        )
        changed += int(getattr(done, "rowcount", 0) or 0)
    return Outcome("written" if changed else "current", changed)


async def score_owner(
    open_tx: OpenTx,
    owner: UUID,
    *,
    settings: Settings,
    table: TableState,
    now: datetime | None = None,
    between: Callable[[], Awaitable[None]] | None = None,
) -> Outcome:
    """One owner's pass. ``between`` runs after the read and before the write; a job passes its
    checkpoint, a test interleaves a purge or an ingest."""
    moment = now or datetime.now(UTC)
    async with open_tx() as db:
        read = await gather(db, owner, settings=settings, table=table, now=moment)
    if not isinstance(read, Gathered):
        return Outcome(read.status)
    if between is not None:
        await between()
    async with open_tx() as db:
        return await _write(db, owner, read, settings=settings, table=table, now=moment)
