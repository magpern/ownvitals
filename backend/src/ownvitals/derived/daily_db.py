"""The database side of the daily pass (#186): read the observations, compute, verify, write.

``daily_owner`` is one owner's pass, in two transactions opened by ``open_tx`` (a job passes its
fenced transaction, a test a user session):

1. **Read** the ``measures`` revision *first*, then the observations of the metrics this pass
   owns (``daily.KEYS``, rows with a source type). If any of them carries another revision, the
   normaliser has not caught up with the source (``behind``; it queues this pass when it has).
   If every stored daily row already carries this revision, fingerprint and algorithm version,
   nothing changed (``current``).
2. **Write**, and in the same transaction first take an owner-scoped advisory lock (two passes
   for one owner queue up; neither can hit a unique violation), then lock the revision row
   ``FOR SHARE`` (an ingest bump or the purge of imported data waits for this transaction) and
   verify that the revision and the fingerprint of the observations are the ones read. Otherwise
   nothing is written (``stale``). Same pattern as ``normalize_db``, for the same reason (the
   purge race of the review of #225).

The diff is applied by natural key ``(metric_key, day)``, **only to rows of the metric keys this
pass owns**: missing days inserted, changed days updated, days with no reading any more deleted,
and the provenance of the unchanged days moved with one statement. Rows of any other
``metric_key`` are never read, deleted or re-stamped. Running twice gives identical rows.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.derived.daily import (
    ALGO_VERSION,
    KEYS,
    DailyRow,
    ObservationRow,
    compute,
    fingerprint,
)

STREAM: Final = "measures"
CHUNK: Final = 500

OpenTx = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_REVISION = text("SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s")
_REVISION_LOCK = text(
    "SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s FOR SHARE"
)
_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))")
# Only the rows this pass reads: the measures-derived observations of its own metric keys.
# Observations of another stream (sleep, activity) carry that stream's own revision and may have
# no group id, so they must neither count as "behind" nor reach ``int(source_grpid)``.
_OBSERVATIONS = text(
    "SELECT metric_key, source_grpid, source_type, source_position, ts, source_tz, value, "
    "quality_flags FROM observations WHERE owner_id = :o "
    "AND metric_key = ANY(CAST(:keys AS text[])) AND source_type IS NOT NULL"
)
_BEHIND = text(
    "SELECT count(*) FROM observations WHERE owner_id = :o "
    "AND metric_key = ANY(CAST(:keys AS text[])) AND source_type IS NOT NULL "
    "AND input_revision <> :r"
)
_STATE = text(
    "SELECT count(*) AS total, count(*) FILTER (WHERE algo_version = :a AND "
    "input_revision = :r AND input_fingerprint = :f) AS current "
    "FROM metric_daily WHERE owner_id = :o AND metric_key = ANY(CAST(:keys AS text[]))"
)
_EXISTING = text(
    "SELECT metric_key, day, method, value, morning, mean, min, max, mad, n, source_groups "
    "FROM metric_daily WHERE owner_id = :o AND metric_key = ANY(CAST(:keys AS text[]))"
)
_INSERT = text(
    "INSERT INTO metric_daily (owner_id, metric_key, day, method, value, morning, mean, min, "
    "max, mad, n, source_groups, algo_version, input_revision, input_fingerprint) VALUES "
    "(:o, :metric_key, :day, :method, :value, :morning, :mean, :min, :max, :mad, :n, "
    "CAST(:groups AS bigint[]), :a, :r, :f)"
)
_UPDATE = text(
    "UPDATE metric_daily SET method = :method, value = :value, morning = :morning, "
    "mean = :mean, min = :min, max = :max, mad = :mad, n = :n, "
    "source_groups = CAST(:groups AS bigint[]), algo_version = :a, input_revision = :r, "
    "input_fingerprint = :f WHERE owner_id = :o AND metric_key = :metric_key AND day = :day"
)
_DELETE = text(
    "DELETE FROM metric_daily WHERE owner_id = :o AND metric_key = :metric_key AND day = :day"
)
_PROVENANCE = text(
    "UPDATE metric_daily SET algo_version = :a, input_revision = :r, input_fingerprint = :f "
    "WHERE owner_id = :o AND metric_key = ANY(CAST(:keys AS text[])) "
    "AND (algo_version, input_revision, input_fingerprint) "
    "IS DISTINCT FROM (CAST(:a AS text), CAST(:r AS bigint), CAST(:f AS bytea))"
)


@dataclass(frozen=True, slots=True)
class Outcome:
    """``written``, ``current`` (nothing changed), ``stale`` (the inputs moved; nothing written,
    a newer pass is queued by whatever moved them), ``behind`` (the observations are not yet
    those of the current source revision; the normaliser queues a pass when it has caught up) or
    ``nothing`` (no revision row: no import, or it was purged). ``affected`` lists the
    ``(metric_key, day)`` pairs inserted, updated or deleted."""

    status: str
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0
    affected: tuple[tuple[str, date], ...] = ()


@dataclass(frozen=True, slots=True)
class Inputs:
    revision: int
    fingerprint: bytes
    rows: tuple[DailyRow, ...]
    skipped: int


def _observations(rows: Any) -> list[ObservationRow]:
    return [
        ObservationRow(
            metric_key=r.metric_key,
            source_grpid=int(r.source_grpid),
            source_type=int(r.source_type),
            source_position=None if r.source_position is None else int(r.source_position),
            ts=r.ts,
            source_tz=str(r.source_tz),
            value=r.value,
            quality_flags=tuple(r.quality_flags),
        )
        for r in rows
    ]


async def _revision(db: AsyncSession, owner: UUID, *, lock: bool) -> int:
    row = (
        await db.execute(_REVISION_LOCK if lock else _REVISION, {"o": owner, "s": STREAM})
    ).first()
    return 0 if row is None else int(row.revision)


async def _read_observations(db: AsyncSession, owner: UUID) -> list[ObservationRow]:
    return _observations((await db.execute(_OBSERVATIONS, {"o": owner, "keys": list(KEYS)})).all())


async def _read(db: AsyncSession, owner: UUID) -> Inputs | Outcome:
    revision = await _revision(db, owner, lock=False)
    if revision == 0:
        return Outcome("nothing")
    if (
        await db.execute(_BEHIND, {"o": owner, "r": revision, "keys": list(KEYS)})
    ).scalar_one() > 0:
        return Outcome("behind")
    observations = await _read_observations(db, owner)
    digest = fingerprint(observations)
    computed = compute(observations)
    state = (
        await db.execute(
            _STATE, {"o": owner, "keys": list(KEYS), "a": ALGO_VERSION, "r": revision, "f": digest}
        )
    ).one()
    if state.current == state.total and state.total == len(computed.rows):
        return Outcome("current", skipped=computed.skipped)
    return Inputs(revision, digest, computed.rows, computed.skipped)


def _params(owner: UUID, row: DailyRow, revision: int, digest: bytes) -> dict[str, Any]:
    return {
        "o": owner,
        "metric_key": row.metric_key,
        "day": row.day,
        "method": row.method,
        "value": row.value,
        "morning": row.morning,
        "mean": row.mean,
        "min": row.minimum,
        "max": row.maximum,
        "mad": row.mad,
        "n": row.n,
        "groups": list(row.source_groups),
        "a": ALGO_VERSION,
        "r": revision,
        "f": digest,
    }


def _stored(row: Any) -> tuple[Any, ...]:
    return (
        row.method,
        row.value,
        row.morning,
        row.mean,
        row.min,
        row.max,
        row.mad,
        row.n,
        tuple(row.source_groups),
    )


def _content(row: DailyRow) -> tuple[Any, ...]:
    return (
        row.method,
        row.value,
        row.morning,
        row.mean,
        row.minimum,
        row.maximum,
        row.mad,
        row.n,
        row.source_groups,
    )


async def _chunks(db: AsyncSession, statement: Any, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), CHUNK):
        await db.execute(statement, rows[start : start + CHUNK])


async def _write(db: AsyncSession, owner: UUID, inputs: Inputs) -> Outcome:
    # Serialise the passes of one owner, then lock the revision row: an ingest bump or a purge
    # of imported data waits for this transaction. The existing rows are read after both.
    await db.execute(_LOCK, {"k": f"metric_daily:{owner}"})
    if await _revision(db, owner, lock=True) != inputs.revision:
        return Outcome("stale")
    if fingerprint(await _read_observations(db, owner)) != inputs.fingerprint:
        return Outcome("stale")
    keys = {"o": owner, "keys": list(KEYS)}
    existing = {(r.metric_key, r.day): r for r in (await db.execute(_EXISTING, keys)).all()}
    desired = {row.key: row for row in inputs.rows}
    revision, digest = inputs.revision, inputs.fingerprint
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for key in sorted(desired):
        old = existing.get(key)
        if old is None:
            inserts.append(_params(owner, desired[key], revision, digest))
        elif _stored(old) != _content(desired[key]):
            updates.append(_params(owner, desired[key], revision, digest))
    gone = sorted(key for key in existing if key not in desired)
    await _chunks(db, _DELETE, [{"o": owner, "metric_key": k, "day": d} for k, d in gone])
    await _chunks(db, _UPDATE, updates)
    await _chunks(db, _INSERT, inserts)
    await db.execute(
        _PROVENANCE, {"o": owner, "keys": list(KEYS), "a": ALGO_VERSION, "r": revision, "f": digest}
    )
    affected = sorted({(p["metric_key"], p["day"]) for p in (*inserts, *updates)} | set(gone))
    return Outcome(
        "written",
        inserted=len(inserts),
        updated=len(updates),
        deleted=len(gone),
        skipped=inputs.skipped,
        affected=tuple(affected),
    )


async def daily_owner(
    open_tx: OpenTx,
    owner: UUID,
    *,
    between: Callable[[], Awaitable[None]] | None = None,
) -> Outcome:
    """One owner's pass (see the module docstring). ``between`` runs after the read and before
    the write; a job passes its checkpoint, a test interleaves a purge or an ingest."""
    async with open_tx() as db:
        read = await _read(db, owner)
    if isinstance(read, Outcome):
        return read
    if between is not None:
        await between()
    async with open_tx() as db:
        return await _write(db, owner, read)
