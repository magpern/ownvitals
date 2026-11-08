"""The input sources of normalisation (#185, #229): what each stream reads and how it computes.

Split from ``normalize_db`` (no behaviour change; its docstring describes the passes). A
``Source`` is data plus callables: the statements that read one stream's rows and the pure
functions that fingerprint and compute them. Reads only, always filtered by ``owner_id``.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

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

STREAM: Final = "measures"

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
