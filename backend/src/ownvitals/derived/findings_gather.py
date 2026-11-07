"""The read side of the rule evaluation (#187): everything one pass reads, in one transaction.

Split from ``findings_store`` (no behaviour change): the module docstring there describes the
whole pass; this holds ``gather`` and its statements. No statement here touches ``findings``.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.quality import EXCLUDING, Reading
from ownvitals.analysis.rules.body import Segment
from ownvitals.analysis.rules.core import Provenance
from ownvitals.analysis.rules.system import ConnectionState, DeviceState, StreamState
from ownvitals.config import Settings
from ownvitals.connections.state import live_connections
from ownvitals.derived import daily
from ownvitals.derived.daily_db import read_day_tz, read_observations
from ownvitals.derived.evaluate import (
    FOOT_PAIRS,
    NERVE_KEYS,
    READING_KEYS,
    SEGMENT_TYPE,
    Inputs,
    Q1State,
    fingerprint,
)
from ownvitals.flags import effective

STREAM: Final = "measures"
CHUNK: Final = 500
SUPPORTED: Final = "supported"
# The extra measure types read as readings, not daily values: B6's segmental muscle, N5's feet.
EXTRA_TYPES: Final = (
    SEGMENT_TYPE,
    *sorted({int(key.removeprefix("meastype_")) for pair in FOOT_PAIRS.values() for key in pair}),
)
# A sync job that is importing a connection's history (Q1's "not while importing").
IMPORT_KINDS: Final = ("withings_backfill", "withings_connect")
_EXCLUDING = frozenset(flag.value for flag in EXCLUDING)

_REVISION = text("SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s")
_REVISION_LOCK = text(
    "SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s FOR SHARE"
)
_BEHIND = text(
    "SELECT count(*) FROM observations WHERE owner_id = :o AND source_type IS NOT NULL "
    "AND (metric_key = ANY(CAST(:keys AS text[])) OR source_type = ANY(CAST(:types AS int[]))) "
    "AND input_revision <> :r"
)
_EXTRA = text(
    "SELECT metric_key, source_grpid, source_type, source_position, ts, source_tz, value, "
    "quality_flags FROM observations WHERE owner_id = :o "
    "AND source_type = ANY(CAST(:types AS int[])) AND source_grpid IS NOT NULL"
)
_DAILY_STATE = text(
    "SELECT count(*) AS total, count(*) FILTER (WHERE algo_version = :a AND "
    "input_revision = :r AND input_fingerprint = :f) AS current "
    "FROM metric_daily WHERE owner_id = :o AND metric_key = ANY(CAST(:keys AS text[]))"
)
_DAILY = text(
    "SELECT metric_key, day, method, value, n, min, max, mad, source_groups FROM metric_daily "
    "WHERE owner_id = :o AND metric_key = ANY(CAST(:keys AS text[]))"
)
_ATTRIB = text(
    "SELECT grpid, max(attrib) AS attrib FROM measure_groups WHERE owner_id = :o "
    "AND deleted_at IS NULL AND grpid = ANY(CAST(:g AS bigint[])) GROUP BY grpid"
)
_CURSORS = text(
    "SELECT connection_id, stream, capability, backfill_state->>'status' AS state "
    "FROM sync_cursors WHERE owner_id = :o AND connection_id = ANY(CAST(:c AS uuid[]))"
)
_DEVICES = text(
    "SELECT hash_deviceid, battery FROM devices WHERE owner_id = :o "
    "AND connection_id = ANY(CAST(:c AS uuid[]))"
)
_IMPORTING = text(
    "SELECT input->>'connection_id' AS connection, input->>'stream' AS stream FROM jobs "
    "WHERE owner_id = :o AND state IN ('queued', 'running') "
    "AND kind = ANY(CAST(:kinds AS text[]))"
)


@dataclass(frozen=True, slots=True)
class Outcome:
    """``written`` (something changed), ``current`` (the stored rows were already these),
    ``stale`` (the inputs moved between read and write; nothing written, whatever moved them
    queues a newer pass), ``behind`` (the observations or the daily values are not yet those of
    the current revision) or ``nothing`` (no revision row). The counts are of rows; ``pending``
    is how many inserted rows are marked to notify."""

    status: str
    inserted: int = 0
    updated: int = 0
    ended: int = 0
    deleted: int = 0
    pending: int = 0
    evaluated: Mapping[str, int] | None = None


@dataclass(frozen=True, slots=True)
class Gathered:
    revision: int
    fingerprint: bytes
    inputs: Inputs


# --- read --------------------------------------------------------------------------------------


async def read_revision(db: AsyncSession, owner: UUID, *, lock: bool) -> int:
    row = (
        await db.execute(_REVISION_LOCK if lock else _REVISION, {"o": owner, "s": STREAM})
    ).first()
    return 0 if row is None else int(row.revision)


def _reading(
    grpid: int, ts: datetime, zone: str, value: Decimal, attrib: int = 0
) -> Reading | None:
    try:
        return Reading(grpid=grpid, measured_at=ts, source_tz=zone, attrib=attrib, value=value)
    except (ValueError, TypeError):
        return None


def _today(now: datetime, day_tz: str | None, latest_zone: str | None) -> date:
    """ADR 0010 §1: the date of ``now`` in the fixed zone, or in per-reading mode in the zone of
    the most recent usable reading (UTC when there is none)."""
    zone = day_tz or latest_zone or "UTC"
    try:
        return now.astimezone(ZoneInfo(zone)).date()
    except (ValueError, KeyError, OSError):
        return now.astimezone(UTC).date()


async def _q1_state(db: AsyncSession, owner: UUID) -> Q1State:
    connections = await live_connections(db, owner)
    ids = [r.id for r in connections]
    if not ids:
        return Q1State()
    cursors = (await db.execute(_CURSORS, {"o": owner, "c": ids})).all()
    devices = (await db.execute(_DEVICES, {"o": owner, "c": ids})).all()
    jobs = (await db.execute(_IMPORTING, {"o": owner, "kinds": list(IMPORT_KINDS)})).all()
    importing = {(j.connection, j.stream) for j in jobs}
    reconnect = any(r.status == "needs_reconnect" for r in connections)
    status = "needs_reconnect" if reconnect else "connected"
    by_stream: dict[str, list[Any]] = defaultdict(list)
    for cursor in cursors:
        by_stream[str(cursor.stream)].append(cursor)
    streams: list[StreamState] = []
    for name, rows in sorted(by_stream.items()):
        supported = [r for r in rows if r.capability == SUPPORTED]
        capability = SUPPORTED if supported else (rows[0].capability if len(rows) == 1 else None)
        streams.append(
            StreamState(
                stream=name,
                capability=capability,
                complete=all(r.state == "complete" for r in supported or rows),
                importing=any(
                    (str(r.connection_id), name) in importing
                    or (str(r.connection_id), None) in importing
                    for r in rows
                ),
            )
        )
    measures = next((s for s in streams if s.stream == STREAM), None)
    batteries: dict[str, str | None] = {}
    for device in devices:
        name = str(device.hash_deviceid)
        if batteries.get(name) != "low":  # one device on two connections: low wins
            batteries[name] = None if device.battery is None else str(device.battery)
    return Q1State(
        weight_capability=None if measures is None else measures.capability,
        connection=ConnectionState(status),
        devices=tuple(DeviceState(name, battery) for name, battery in sorted(batteries.items())),
        streams=tuple(streams),
    )


type DailyByKey = dict[str, tuple[DailyValue, ...]]
type GroupsByKey = dict[str, dict[date, tuple[int, ...]]]


def _daily_values(rows: Iterable[Any]) -> tuple[DailyByKey, GroupsByKey]:
    values: dict[str, list[DailyValue]] = defaultdict(list)
    groups: dict[str, dict[date, tuple[int, ...]]] = defaultdict(dict)
    for r in rows:
        values[r.metric_key].append(
            DailyValue(
                day=r.day,
                value=r.value,
                method=DailyMethod(r.method),
                n=int(r.n),
                minimum=r.min,
                maximum=r.max,
                mad=r.mad,
            )
        )
        groups[r.metric_key][r.day] = tuple(int(g) for g in r.source_groups)
    return (
        {k: tuple(sorted(v, key=lambda d: d.day)) for k, v in values.items()},
        dict(groups),
    )


async def gather(
    db: AsyncSession, owner: UUID, *, settings: Settings, now: datetime
) -> Gathered | Outcome:
    """Everything one pass reads, in one transaction (see the module docstring)."""
    revision = await read_revision(db, owner, lock=False)
    if revision == 0:
        return Outcome("nothing")
    keys = list(daily.KEYS)
    behind = await db.execute(
        _BEHIND, {"o": owner, "keys": keys, "types": list(EXTRA_TYPES), "r": revision}
    )
    if behind.scalar_one() > 0:
        return Outcome("behind")
    day_tz = await read_day_tz(db, owner)
    observations = await read_observations(db, owner)
    expected = daily.compute(observations, day_tz=day_tz)
    state = (
        await db.execute(
            _DAILY_STATE,
            {
                "o": owner,
                "keys": keys,
                "a": daily.ALGO_VERSION,
                "r": revision,
                "f": daily.fingerprint(observations, day_tz=day_tz),
            },
        )
    ).one()
    if not (state.current == state.total == len(expected.rows)):
        return Outcome("behind")
    values, groups = _daily_values((await db.execute(_DAILY, {"o": owner, "keys": keys})).all())

    usable = [o for o in observations if not _EXCLUDING & set(o.quality_flags)]
    jumped = {(o.metric_key, o.source_grpid) for o in usable if "jump" in o.quality_flags}
    provenance: dict[str, dict[date, Provenance]] = {
        key: {
            day: Provenance(gs, any((key, g) in jumped for g in gs)) for day, gs in by_day.items()
        }
        for key, by_day in groups.items()
    }
    nerve_groups = sorted({o.source_grpid for o in usable if o.metric_key in NERVE_KEYS})
    attribs = {
        int(r.grpid): int(r.attrib or 0)
        for r in (await db.execute(_ATTRIB, {"o": owner, "g": nerve_groups})).all()
    }
    extra = (await db.execute(_EXTRA, {"o": owner, "types": list(EXTRA_TYPES)})).all()

    readings: dict[str, list[Reading]] = defaultdict(list)
    seen: set[tuple[str, int]] = set()
    for o in sorted(usable, key=lambda o: (o.ts, o.source_grpid)):
        if o.metric_key not in READING_KEYS or (o.metric_key, o.source_grpid) in seen:
            continue
        reading = _reading(
            o.source_grpid,
            o.ts,
            day_tz or o.source_tz,
            o.value,
            attribs.get(o.source_grpid, 0) if o.metric_key in NERVE_KEYS else 0,
        )
        if reading is not None:
            seen.add((o.metric_key, o.source_grpid))
            readings[o.metric_key].append(reading)
    segments: list[Segment] = []
    for row in sorted(extra, key=lambda r: (r.ts, int(r.source_grpid), r.metric_key)):
        if _EXCLUDING & set(row.quality_flags):
            continue
        reading = _reading(int(row.source_grpid), row.ts, day_tz or str(row.source_tz), row.value)
        if reading is None:
            continue
        if int(row.source_type) == SEGMENT_TYPE:
            if row.source_position is not None:
                segments.append(Segment(int(row.source_position), reading))
        elif (row.metric_key, reading.grpid) not in seen:
            seen.add((row.metric_key, reading.grpid))
            readings[str(row.metric_key)].append(reading)

    latest = max(usable, key=lambda o: (o.ts, o.source_grpid), default=None)
    inputs = Inputs(
        today=_today(now, day_tz, None if latest is None else latest.source_tz),
        experimental=await effective.experimental_findings(db, settings),
        daily=values,
        provenance=provenance,
        readings={k: tuple(v) for k, v in readings.items()},
        segments=tuple(segments),
        q1=await _q1_state(db, owner),
    )
    return Gathered(revision, fingerprint(inputs), inputs)


# --- write -------------------------------------------------------------------------------------
