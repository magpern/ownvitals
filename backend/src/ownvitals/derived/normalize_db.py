"""The database side of normalisation (#185, #229): read the inputs, compute, verify, write.

A **source** (``Source``) is one input stream of ``observations``: ``measures`` (#185), and the
``sleep_summary``, ``activity_daily`` and ``workouts`` streams (#229). ``normalise_owner`` is one
owner's pass over one source, in three phases, each in its own transaction opened by ``open_tx``
(a job passes its fenced transaction, a test a user session); ``normalise_all`` runs every source.

1. **Read** the source's ``data_revisions`` revision *first*, then its rows. If the stored
   observations the source owns already carry this revision, fingerprint and algorithm version,
   nothing changed: done (``current``).
2. **Compute** (pure, ``derived.normalize`` and ``derived.normalize_streams``) and create the
   month partitions the new rows need, one short committed transaction per month (ADR 0002 §2
   rule 6; the function takes an advisory lock and must not run inside the insert transaction).
3. **Write**, and in the same transaction first take the owner's normalisation lock (so two
   passes of one owner never write at once), then **verify** that the inputs are still the ones
   read: the ``data_revisions`` row is locked ``FOR SHARE`` (an ingest bump or the purge of
   imported data waits for this transaction), its revision must equal the one read, and the
   fingerprint of the source rows must equal the one read. Otherwise nothing is written
   (``stale``). This is what stops a pass that read before a "delete imported data" purge from
   writing rows after it (issue #187's comment, from the review of #225). Any change to the
   inputs also queues a new pass, so a ``stale`` pass needs no retry. A purge removes the
   revision row: a pass that finds none writes nothing (``nothing``).

**Ownership.** Each source owns a disjoint part of the owner's ``observations``: measures the rows
with a ``source_type``, each stream the rows with none under its key prefix (``sleep_``,
``activity_``, ``workout_``). Every statement of the diff (``_OWNED``) is limited to that part, and
a computed row outside it stops the pass, so no pass deletes or re-stamps another writer's rows.

The diff is applied by natural key: missing rows inserted, changed rows updated, rows with no
source (a soft-deleted group, a backdated instant, a replaced summary) deleted, and the
provenance of the rest moved to the new revision with one statement. Running twice gives
identical rows.
"""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.db.queue_locks import NORMALISATION
from ownvitals.derived import normalize_streams as streams
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

# One normalisation write per owner at a time (namespace 6, ADR 0005 section 9): a live and a
# backfill pass of the same owner would otherwise both insert the same keys, and the second one
# would fail on ``observations_source_key`` (#229).
_LOCK = text("SELECT pg_catalog.pg_advisory_xact_lock(:ns, pg_catalog.hashtext(CAST(:o AS text)))")
_REVISION = text("SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s")
_REVISION_LOCK = text(
    "SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s FOR SHARE"
)
# category 2 is the user's objectives (goals), not readings; an absent category is a reading.
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
_SLEEP_ROWS = text(
    "SELECT id, payload_hash, false AS deleted FROM sleep_summaries WHERE owner_id = :o"
)
_SLEEP = text("SELECT start_at, end_at, source_tz, raw FROM sleep_summaries WHERE owner_id = :o")
_ACTIVITY_ROWS = text(
    "SELECT id, payload_hash, false AS deleted FROM activity_daily WHERE owner_id = :o"
)
_ACTIVITY = text("SELECT day, start_at, source_tz, raw FROM activity_daily WHERE owner_id = :o")
_WORKOUT_ROWS = text("SELECT id, payload_hash, false AS deleted FROM workouts WHERE owner_id = :o")
_WORKOUT = text("SELECT workout_id, start_at, source_tz, raw FROM workouts WHERE owner_id = :o")
_INSERT = text(
    "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
    "source_value_raw, source_unit, conversion, quality_flags, source_grpid, source_type, "
    "source_position, algo_version, input_revision, input_fingerprint) VALUES "
    "(:o, :metric_key, :ts, :source_ts, :source_tz, :value, :raw, :unit, :conversion, "
    "CAST(:flags AS text[]), :grpid, :type, :position, :a, :r, :f)"
)


# The rows a pass owns (#229): ``:p`` is the source's key prefix, NULL for measures. Measures own
# the rows that name a measure type; a stream owns the rows with none under its prefix
# (``starts_with``, since ``_`` is a LIKE wildcard). With ``:p`` NULL the second branch is NULL,
# with ``:p`` set the first is false, so the parts are disjoint. Every statement of the diff
# carries it, written out each time: a pass never reads as its own, updates, deletes or
# re-stamps another writer's rows.
_STATE = text(
    "SELECT count(*) AS total, count(*) FILTER (WHERE algo_version = :a AND "
    "input_revision = :r AND input_fingerprint = :f) AS current FROM observations WHERE "
    "owner_id = :o AND ((CAST(:p AS text) IS NULL AND source_type IS NOT NULL) "
    "OR (source_type IS NULL AND starts_with(metric_key, CAST(:p AS text))))"
)
_MONTHS = text(
    "SELECT DISTINCT date_trunc('month', ts AT TIME ZONE 'UTC')::date AS month "
    "FROM observations WHERE "
    "owner_id = :o AND ((CAST(:p AS text) IS NULL AND source_type IS NOT NULL) "
    "OR (source_type IS NULL AND starts_with(metric_key, CAST(:p AS text))))"
)
_EXISTING = text(
    "SELECT id, metric_key, ts, source_ts, source_tz, value, source_value_raw, source_unit, "
    "conversion, quality_flags, source_grpid, source_type, source_position "
    "FROM observations WHERE "
    "owner_id = :o AND ((CAST(:p AS text) IS NULL AND source_type IS NOT NULL) "
    "OR (source_type IS NULL AND starts_with(metric_key, CAST(:p AS text))))"
)
_UPDATE = text(
    "UPDATE observations SET source_ts = :source_ts, source_tz = :source_tz, value = :value, "
    "source_value_raw = :raw, source_unit = :unit, conversion = :conversion, "
    "quality_flags = CAST(:flags AS text[]), algo_version = :a, input_revision = :r, "
    "input_fingerprint = :f WHERE "
    "owner_id = :o AND ((CAST(:p AS text) IS NULL AND source_type IS NOT NULL) "
    "OR (source_type IS NULL AND starts_with(metric_key, CAST(:p AS text)))) "
    "AND id = :id AND ts = :ts"
)
_DELETE = text(
    "DELETE FROM observations WHERE "
    "owner_id = :o AND ((CAST(:p AS text) IS NULL AND source_type IS NOT NULL) "
    "OR (source_type IS NULL AND starts_with(metric_key, CAST(:p AS text)))) "
    "AND id = ANY(:ids)"
)
_PROVENANCE = text(
    "UPDATE observations SET algo_version = :a, input_revision = :r, input_fingerprint = :f "
    "WHERE "
    "owner_id = :o AND ((CAST(:p AS text) IS NULL AND source_type IS NOT NULL) "
    "OR (source_type IS NULL AND starts_with(metric_key, CAST(:p AS text)))) "
    "AND (algo_version, input_revision, input_fingerprint) "
    "IS DISTINCT FROM (CAST(:a AS text), CAST(:r AS bigint), CAST(:f AS bytea))"
)


@dataclass(frozen=True, slots=True)
class Source:
    """One input stream of ``observations``: its ``data_revisions`` stream, its algorithm
    version, the rows it owns, how it fingerprints and reads its inputs, and how it computes."""

    stream: str
    algo_version: str
    prefix: str | None  # the ``:p`` of ``_OWNED``
    claims: Callable[[Observation], bool]
    inputs: TextClause
    digest: Callable[[Sequence[GroupRef]], bytes]
    load: Callable[[AsyncSession, UUID], Awaitable[tuple[Any, ...]]]
    compute: Callable[[tuple[Any, ...], datetime], Normalised]


async def _load_measures(db: AsyncSession, owner: UUID) -> tuple[SourceMeasure, ...]:
    rows = (await db.execute(_MEASURES, {"o": owner})).all()
    return tuple(
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


async def _load_sleep(db: AsyncSession, owner: UUID) -> tuple[streams.StreamRecord, ...]:
    rows = (await db.execute(_SLEEP, {"o": owner})).all()
    return tuple(
        streams.StreamRecord(
            ref=streams.ref_of(r.raw),
            ts=r.end_at,
            source_ts=r.start_at,
            source_tz=str(r.source_tz),
            fields=streams.fields_of(r.raw, "data"),
        )
        for r in rows
    )


async def _load_activity(db: AsyncSession, owner: UUID) -> tuple[streams.StreamRecord, ...]:
    rows = (await db.execute(_ACTIVITY, {"o": owner})).all()
    return tuple(
        streams.StreamRecord(
            ref=None,
            ts=None,
            source_ts=r.start_at,
            source_tz=str(r.source_tz),
            fields=streams.fields_of(r.raw, "fields"),
            noon_of=r.day,
        )
        for r in rows
    )


async def _load_workouts(db: AsyncSession, owner: UUID) -> tuple[streams.StreamRecord, ...]:
    rows = (await db.execute(_WORKOUT, {"o": owner})).all()
    return tuple(
        streams.StreamRecord(
            ref=int(r.workout_id),
            ts=r.start_at,
            source_ts=r.start_at,
            source_tz=str(r.source_tz),
            fields=streams.fields_of(r.raw, "data"),
        )
        for r in rows
    )


def _stream_source(
    name: str,
    spec: streams.Stream,
    inputs: TextClause,
    load: Callable[[AsyncSession, UUID], Awaitable[tuple[Any, ...]]],
) -> Source:
    def digest(rows: Sequence[GroupRef]) -> bytes:
        return streams.stream_fingerprint(name, rows)

    def compute(records: tuple[Any, ...], now: datetime) -> Normalised:
        return streams.normalise_stream(records, spec, now=now)

    def claims(row: Observation) -> bool:
        return row.source_type is None and streams.owned_key(spec.prefix, row.metric_key)

    return Source(
        stream=name,
        algo_version=streams.ALGO_VERSION,
        prefix=spec.prefix,
        claims=claims,
        inputs=inputs,
        digest=digest,
        load=load,
        compute=compute,
    )


# The measures pass owns the rows that name a measure type; no stream row does.
MEASURES: Final = Source(
    stream=STREAM,
    algo_version=ALGO_VERSION,
    prefix=None,
    claims=lambda row: row.source_type is not None,
    inputs=_GROUPS,
    digest=fingerprint,
    load=_load_measures,
    compute=lambda measures, now: normalise(measures, now=now),
)
SLEEP: Final = _stream_source("sleep_summary", streams.SLEEP, _SLEEP_ROWS, _load_sleep)
ACTIVITY: Final = _stream_source("activity_daily", streams.ACTIVITY, _ACTIVITY_ROWS, _load_activity)
WORKOUTS: Final = _stream_source("workouts", streams.WORKOUT, _WORKOUT_ROWS, _load_workouts)
# Every source, in the order a job runs them. Their ``data_revisions`` streams are the ones whose
# ingest queues a pass (``normalize_job.TRIGGERS``).
SOURCES: Final = (MEASURES, SLEEP, ACTIVITY, WORKOUTS)


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
    records: tuple[Any, ...]
    months: frozenset[date]


async def _revision(db: AsyncSession, owner: UUID, stream: str, *, lock: bool) -> int:
    row = (
        await db.execute(_REVISION_LOCK if lock else _REVISION, {"o": owner, "s": stream})
    ).first()
    return 0 if row is None else int(row.revision)


async def _fingerprint(db: AsyncSession, owner: UUID, source: Source) -> bytes:
    rows = (await db.execute(source.inputs, {"o": owner})).all()
    return source.digest([GroupRef(r.id, bytes(r.payload_hash), bool(r.deleted)) for r in rows])


async def _read(db: AsyncSession, owner: UUID, source: Source) -> Inputs | Outcome:
    revision = await _revision(db, owner, source.stream, lock=False)
    if revision == 0:
        return Outcome("nothing")
    digest = await _fingerprint(db, owner, source)
    state = (
        await db.execute(
            _STATE,
            {"o": owner, "p": source.prefix, "a": source.algo_version, "r": revision, "f": digest},
        )
    ).one()
    if state.total > 0 and state.current == state.total:
        return Outcome("current")
    records = await source.load(db, owner)
    months = (await db.execute(_MONTHS, {"o": owner, "p": source.prefix})).scalars().all()
    return Inputs(revision, digest, records, frozenset(months))


def _params(
    owner: UUID, row: Observation, source: Source, revision: int, digest: bytes
) -> dict[str, Any]:
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
        "a": source.algo_version,
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


async def _write(
    db: AsyncSession, owner: UUID, source: Source, inputs: Inputs, normalised: Normalised
) -> Outcome:
    # The owner's normalisation lock first, so two passes never diff against the same rows at
    # once; then the revision row, so an ingest bump or a purge of imported data waits for this
    # transaction. Nothing else takes the advisory lock, so the order makes no cycle.
    await db.execute(_LOCK, {"ns": NORMALISATION, "o": owner})
    if await _revision(db, owner, source.stream, lock=True) != inputs.revision:
        return Outcome("stale")
    if await _fingerprint(db, owner, source) != inputs.fingerprint:
        return Outcome("stale")
    owned = {"o": owner, "p": source.prefix}
    existing = {}
    for r in (await db.execute(_EXISTING, owned)).all():
        existing[(r.metric_key, r.source_grpid, r.source_type, r.source_position, r.ts)] = r
    desired = {row.natural_key: row for row in normalised.observations}
    revision, digest = inputs.revision, inputs.fingerprint
    inserts: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    for key, row in desired.items():
        old = existing.get(key)
        if old is None:
            inserts.append(_params(owner, row, source, revision, digest))
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
            updates.append({**_params(owner, row, source, revision, digest), **owned, "id": old.id})
    gone = [old.id for key, old in existing.items() if key not in desired]
    for start in range(0, len(gone), CHUNK):
        await db.execute(_DELETE, {**owned, "ids": gone[start : start + CHUNK]})
    await _chunks(db, _UPDATE, updates)
    await _chunks(db, _INSERT, inserts)
    await db.execute(_PROVENANCE, {**owned, "a": source.algo_version, "r": revision, "f": digest})
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
    source: Source = MEASURES,
    between: Callable[[], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> Outcome:
    """One owner's pass over one source (see the module docstring). ``between`` runs after the
    read and the partitions and before the write; a job passes its checkpoint, a test
    interleaves a purge."""
    async with open_tx() as db:
        read = await _read(db, owner, source)
    if isinstance(read, Outcome):
        return read
    normalised = source.compute(read.records, now or datetime.now(UTC))
    if not all(source.claims(row) for row in normalised.observations):
        raise RuntimeError("a pass computed a row outside the rows it owns")
    needed = {month_of(row.ts) for row in normalised.observations}
    for month in sorted(needed - read.months):
        async with open_tx() as db:
            await ensure_month(db, "observations", month)
    if between is not None:
        await between()
    async with open_tx() as db:
        return await _write(db, owner, source, read, normalised)


async def normalise_all(
    open_tx: OpenTx,
    owner: UUID,
    *,
    between: Callable[[], Awaitable[None]] | None = None,
    now: datetime | None = None,
) -> dict[str, Outcome]:
    """Every source of the owner, one pass each, in ``SOURCES`` order."""
    return {
        source.stream: await normalise_owner(
            open_tx, owner, source=source, between=between, now=now
        )
        for source in SOURCES
    }
