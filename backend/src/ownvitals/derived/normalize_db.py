"""The database side of normalisation (#185): read the inputs, compute, verify, write.

``normalise_owner`` is one owner's pass, in three phases, each in its own transaction opened by
``open_tx`` (a job passes its fenced transaction, a test a user session):

1. **Read** the ``measures`` revision *first*, then the groups and measures. If the stored
   observations already carry this revision, fingerprint and algorithm version, nothing changed:
   done (``current``).
2. **Compute** (pure, ``derived.normalize``) and create the month partitions the new rows need,
   one short committed transaction per month (ADR 0002 §2 rule 6; the function takes an advisory
   lock and must not run inside the insert transaction).
3. **Write**, and in the same transaction first **verify** that the inputs are still the ones
   read: the ``data_revisions`` row is locked ``FOR SHARE`` (an ingest bump or the purge of
   imported data waits for this transaction), its revision must equal the one read, and the
   fingerprint of the groups must equal the one read. Otherwise nothing is written (``stale``).
   This is what stops a pass that read before a "delete imported data" purge from writing rows
   after it (issue #187's comment, from the review of #225). Any change to the inputs also queues
   a new pass, so a ``stale`` pass needs no retry. A purge removes the revision row: a pass that
   finds none writes nothing (``nothing``).

The diff is applied by natural key: missing rows inserted, changed rows updated, rows with no
source (a soft-deleted group, a backdated instant) deleted, and the provenance of the rest moved
to the new revision with one statement. Running twice gives identical rows.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.derived.normalize import (
    ALGO_VERSION,
    GroupRef,
    Normalised,
    Observation,
    SourceMeasure,
    fingerprint,
    normalise,
)
from ownvitals.ingest.partitions import ensure_month
from ownvitals.ingest.when import month_of

STREAM: Final = "measures"
CHUNK: Final = 500

OpenTx = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# category 2 is the user's objectives (goals), not readings; an absent category is a reading.
_REVISION = text("SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s")
_REVISION_LOCK = text(
    "SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s FOR SHARE"
)
_GROUPS = text(
    "SELECT g.id, g.payload_hash, g.deleted_at IS NOT NULL AS deleted FROM measure_groups g "
    "WHERE g.owner_id = :o AND (g.category IS NULL OR g.category = 1)"
)
_MEASURES = text(
    "SELECT g.grpid, g.measured_at, g.source_tz, g.attrib, m.type, m.value_raw, m.unit, "
    "m.position FROM measures m JOIN measure_groups g "
    "ON g.owner_id = m.owner_id AND g.id = m.measure_group_id "
    "WHERE m.owner_id = :o AND g.deleted_at IS NULL AND (g.category IS NULL OR g.category = 1)"
)
_STATE = text(
    "SELECT count(*) AS total, count(*) FILTER (WHERE algo_version = :a AND "
    "input_revision = :r AND input_fingerprint = :f) AS current "
    "FROM observations WHERE owner_id = :o"
)
_MONTHS = text(
    "SELECT DISTINCT date_trunc('month', ts AT TIME ZONE 'UTC')::date AS month "
    "FROM observations WHERE owner_id = :o"
)
_EXISTING = text(
    "SELECT id, metric_key, ts, source_ts, source_tz, value, source_value_raw, source_unit, "
    "conversion, quality_flags, source_grpid, source_type, source_position "
    "FROM observations WHERE owner_id = :o"
)
_INSERT = text(
    "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
    "source_value_raw, source_unit, conversion, quality_flags, source_grpid, source_type, "
    "source_position, algo_version, input_revision, input_fingerprint) VALUES "
    "(:o, :metric_key, :ts, :source_ts, :source_tz, :value, :raw, :unit, :conversion, "
    "CAST(:flags AS text[]), :grpid, :type, :position, :a, :r, :f)"
)
_UPDATE = text(
    "UPDATE observations SET source_ts = :source_ts, source_tz = :source_tz, value = :value, "
    "source_value_raw = :raw, source_unit = :unit, conversion = :conversion, "
    "quality_flags = CAST(:flags AS text[]), algo_version = :a, input_revision = :r, "
    "input_fingerprint = :f WHERE owner_id = :o AND id = :id AND ts = :ts"
)
_DELETE = text("DELETE FROM observations WHERE owner_id = :o AND id = ANY(:ids)")
_PROVENANCE = text(
    "UPDATE observations SET algo_version = :a, input_revision = :r, input_fingerprint = :f "
    "WHERE owner_id = :o AND (algo_version, input_revision, input_fingerprint) "
    "IS DISTINCT FROM (CAST(:a AS text), CAST(:r AS bigint), CAST(:f AS bytea))"
)


@dataclass(frozen=True, slots=True)
class Outcome:
    """``written``, ``current`` (nothing changed), ``stale`` (the inputs moved; nothing written,
    a newer pass is queued by whatever moved them) or ``nothing`` (no revision row: no import,
    or it was purged)."""

    status: str
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class Inputs:
    revision: int
    fingerprint: bytes
    measures: tuple[SourceMeasure, ...]
    months: frozenset[date]


async def _revision(db: AsyncSession, owner: UUID, *, lock: bool) -> int:
    row = (
        await db.execute(_REVISION_LOCK if lock else _REVISION, {"o": owner, "s": STREAM})
    ).first()
    return 0 if row is None else int(row.revision)


async def _fingerprint(db: AsyncSession, owner: UUID) -> bytes:
    rows = (await db.execute(_GROUPS, {"o": owner})).all()
    return fingerprint(GroupRef(r.id, bytes(r.payload_hash), bool(r.deleted)) for r in rows)


async def _read(db: AsyncSession, owner: UUID) -> Inputs | Outcome:
    revision = await _revision(db, owner, lock=False)
    if revision == 0:
        return Outcome("nothing")
    digest = await _fingerprint(db, owner)
    state = (
        await db.execute(_STATE, {"o": owner, "a": ALGO_VERSION, "r": revision, "f": digest})
    ).one()
    if state.total > 0 and state.current == state.total:
        return Outcome("current")
    rows = (await db.execute(_MEASURES, {"o": owner})).all()
    months = (await db.execute(_MONTHS, {"o": owner})).scalars().all()
    measures = tuple(
        SourceMeasure(
            grpid=int(r.grpid),
            measured_at=r.measured_at,
            source_tz=str(r.source_tz),
            attrib=None if r.attrib is None else int(r.attrib),
            type=int(r.type),
            value_raw=int(r.value_raw),
            unit=int(r.unit),
            position=None if r.position is None else int(r.position),
        )
        for r in rows
    )
    return Inputs(revision, digest, measures, frozenset(months))


def _params(owner: UUID, row: Observation, revision: int, digest: bytes) -> dict[str, Any]:
    return {
        "o": owner,
        "metric_key": row.metric_key,
        "ts": row.ts,
        "source_ts": row.source_ts,
        "source_tz": row.source_tz,
        "value": row.value,
        "raw": row.source_value_raw,
        "unit": row.source_unit,
        "conversion": row.conversion,
        "flags": list(row.quality_flags),
        "grpid": row.source_grpid,
        "type": row.source_type,
        "position": row.source_position,
        "a": ALGO_VERSION,
        "r": revision,
        "f": digest,
    }


def _content(
    row: Observation,
) -> tuple[datetime, str, Decimal, int, int, str, tuple[str, ...]]:
    return (
        row.source_ts,
        row.source_tz,
        row.value,
        row.source_value_raw,
        row.source_unit,
        row.conversion,
        row.quality_flags,
    )


async def _chunks(db: AsyncSession, statement: Any, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), CHUNK):
        await db.execute(statement, rows[start : start + CHUNK])


async def _write(db: AsyncSession, owner: UUID, inputs: Inputs, normalised: Normalised) -> Outcome:
    # The lock first: an ingest bump or a purge of imported data waits for this transaction.
    if await _revision(db, owner, lock=True) != inputs.revision:
        return Outcome("stale")
    if await _fingerprint(db, owner) != inputs.fingerprint:
        return Outcome("stale")
    existing = {}
    for r in (await db.execute(_EXISTING, {"o": owner})).all():
        existing[(r.metric_key, r.source_grpid, r.source_type, r.source_position, r.ts)] = r
    desired = {row.natural_key: row for row in normalised.observations}
    revision, digest = inputs.revision, inputs.fingerprint
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for key, row in desired.items():
        old = existing.get(key)
        if old is None:
            inserts.append(_params(owner, row, revision, digest))
            continue
        stored = (
            old.source_ts,
            old.source_tz,
            old.value,
            old.source_value_raw,
            old.source_unit,
            old.conversion,
            tuple(sorted(old.quality_flags)),
        )
        if stored != _content(row):
            updates.append({**_params(owner, row, revision, digest), "id": old.id})
    gone = [old.id for key, old in existing.items() if key not in desired]
    for start in range(0, len(gone), CHUNK):
        await db.execute(_DELETE, {"o": owner, "ids": gone[start : start + CHUNK]})
    await _chunks(db, _UPDATE, updates)
    await _chunks(db, _INSERT, inserts)
    await db.execute(_PROVENANCE, {"o": owner, "a": ALGO_VERSION, "r": revision, "f": digest})
    return Outcome(
        "written",
        inserted=len(inserts),
        updated=len(updates),
        deleted=len(gone),
        skipped=normalised.skipped.total(),
    )


async def normalise_owner(
    open_tx: OpenTx,
    owner: UUID,
    *,
    between: Callable[[], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> Outcome:
    """One owner's pass (see the module docstring). ``between`` runs after the read and the
    partitions and before the write; a job passes its checkpoint, a test interleaves a purge."""
    async with open_tx() as db:
        read = await _read(db, owner)
    if isinstance(read, Outcome):
        return read
    normalised = normalise(read.measures, now=now or datetime.now(UTC))
    needed = {month_of(row.ts) for row in normalised.observations}
    for month in sorted(needed - read.months):
        async with open_tx() as db:
            await ensure_month(db, "observations", month)
    if between is not None:
        await between()
    async with open_tx() as db:
        return await _write(db, owner, read, normalised)
