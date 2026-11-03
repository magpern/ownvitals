"""The database side of the rule evaluation (#187): read the inputs, evaluate, verify, write.

``evaluate_owner`` is one owner's pass, in two transactions opened by ``open_tx`` (a job passes
its fenced transaction, a test a user session). Every statement filters ``owner_id`` itself as
well as running under forced RLS (ADR 0002).

1. **Read** (``gather``) the ``measures`` revision first, then the profile's day setting, the
   observations, the stored ``metric_daily`` rows, the group ``attrib`` of the nerve readings,
   Q1's state records and the owner-only switch. ``nothing`` without a revision row (nothing
   imported, or purged). ``behind`` when an observation read carries another revision (the
   normaliser has not caught up) or the daily rows are not those of these observations and this
   day setting (the daily pass has not caught up; it queues this pass when it has).
2. **Evaluate** (``derived.evaluate``, pure).
3. **Write**, in one transaction: take the owner's normalisation lock (namespace 6, ADR 0005 §9:
   the lock the normaliser's and the daily pass's writes take), lock the revision row ``FOR
   SHARE`` (an ingest bump or the purge of imported data waits for this transaction), and
   **gather the inputs again** after both locks: the revision and the fingerprint must be the
   ones read, else nothing is written (``stale``). Same discipline as ``daily_db`` (the purge
   race of the review of #225).

**The diff** (ADR 0010 §4) touches only the rows of the rules this writer owns
(``evaluate.OWNED_RULES``); another writer's rules (E1 to E6) are never read, changed or
deleted.

* Replayed rules: a stored episode and a computed one with the same rule, subject and reference
  day are the same episode. It keeps its id, ``first_seen``, ``origin``, ``notify_state``,
  ``notified_at`` and ``seen_at``; the rest is updated when it differs. A computed episode with
  no stored match is inserted, with ``origin = origin(window_end, today)`` (computed once, at this
  first write) and ``notify_state`` ``pending`` exactly when that origin is ``live``. A stored
  episode no recompute produces (its data changed, or the owner-only switch is off) is deleted.
* Q1: today's result is applied to the stored active episode of its rule and subject by
  ``advance_simple``: fire and none active, insert (live, so ``pending``); fire while active,
  update (the reference day and origin stay); no fire, or a subject no longer reported (a device
  or a stream gone), end it. Ended Q1 episodes are history and never touched.
* Statements run in the order deletes, ends, other updates, inserts, so the one-active-episode
  index (``findings_active_episode``) is never violated mid-diff.

``first_seen``, ``last_seen`` and ``ended_at`` are timestamps in the schema; the pass stores the
evaluation day of the first firing, the latest firing and the end at 00:00 UTC, so a recompute
of the same data gives byte-identical rows. Provenance: ``input_revision`` is the ``measures``
counter (the one stream the v1 rules read, through observations and daily values; Q1's state
records are not a ``data_revisions`` stream) and ``input_fingerprint`` is
``evaluate.fingerprint`` of the inputs. Every owned row the pass re-derives carries both; an
ended Q1 episode keeps the provenance it ended with.
"""

import json
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.quality import EXCLUDING, Reading
from ownvitals.analysis.rules.body import Segment
from ownvitals.analysis.rules.core import Finding, Provenance
from ownvitals.analysis.rules.episodes import origin
from ownvitals.analysis.rules.system import ConnectionState, DeviceState, StreamState
from ownvitals.config import Settings
from ownvitals.connections.state import live_connections
from ownvitals.db.queue_locks import NORMALISATION
from ownvitals.derived import daily
from ownvitals.derived.daily_db import read_day_tz, read_observations
from ownvitals.derived.evaluate import (
    FOOT_PAIRS,
    NERVE_KEYS,
    OWNED_RULES,
    Q1_RULES,
    READING_KEYS,
    SEGMENT_TYPE,
    EpisodeRow,
    Evaluation,
    Inputs,
    Q1State,
    canonical,
    evaluate,
    fingerprint,
    json_safe,
    rule_version,
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

OpenTx = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_REVISION = text("SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s")
_REVISION_LOCK = text(
    "SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s FOR SHARE"
)
_LOCK = text("SELECT pg_catalog.pg_advisory_xact_lock(:ns, pg_catalog.hashtext(CAST(:o AS text)))")
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
_COLUMNS = (
    "id, rule_id, rule_version, tier, severity, confidence, data_quality, origin, metric_keys, "
    "subject, window_start, window_end, reference_day, evidence, status, first_seen, last_seen, "
    "ended_at"
)
_STORED = text(
    f"SELECT {_COLUMNS} FROM findings WHERE owner_id = :o "  # noqa: S608 - constant columns
    "AND rule_id = ANY(CAST(:rules AS text[]))"
)
_INSERT = text(
    "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
    "data_quality, origin, metric_keys, subject, window_start, window_end, reference_day, "
    "evidence, input_revision, input_fingerprint, status, first_seen, last_seen, ended_at, "
    "notify_state) VALUES (:o, :rule_id, :rule_version, :tier, :severity, :confidence, "
    "CAST(:data_quality AS jsonb), :origin, CAST(:metric_keys AS text[]), :subject, "
    ":window_start, :window_end, :reference_day, CAST(:evidence AS jsonb), :r, :f, :status, "
    ":first_seen, :last_seen, :ended_at, :notify_state)"
)
_UPDATE = text(
    "UPDATE findings SET rule_version = :rule_version, tier = :tier, severity = :severity, "
    "confidence = :confidence, data_quality = CAST(:data_quality AS jsonb), "
    "metric_keys = CAST(:metric_keys AS text[]), window_start = :window_start, "
    "window_end = :window_end, evidence = CAST(:evidence AS jsonb), input_revision = :r, "
    "input_fingerprint = :f, status = :status, last_seen = :last_seen, ended_at = :ended_at "
    "WHERE owner_id = :o AND id = :id"
)
_DELETE = text("DELETE FROM findings WHERE owner_id = :o AND id = :id")
_CLOSE = text(
    "UPDATE findings SET status = 'ended', ended_at = :ended_at "
    "WHERE owner_id = :o AND id = :id AND status = 'active'"
)
# Moves the provenance of every owned row the pass re-derived and left unchanged; an ended Q1
# episode (history, not re-derived) keeps its own.
_PROVENANCE = text(
    "UPDATE findings SET input_revision = :r, input_fingerprint = :f WHERE owner_id = :o "
    "AND rule_id = ANY(CAST(:rules AS text[])) "
    "AND (status = 'active' OR rule_id <> ALL(CAST(:q1 AS text[]))) "
    "AND (input_revision, input_fingerprint) IS DISTINCT FROM (CAST(:r AS bigint), "
    "CAST(:f AS bytea))"
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


def day_start(day: date) -> datetime:
    """The timestamp the pass stores for an evaluation day: 00:00 UTC of it."""
    return datetime.combine(day, time(), UTC)


# --- read --------------------------------------------------------------------------------------


async def _revision(db: AsyncSession, owner: UUID, *, lock: bool) -> int:
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
    revision = await _revision(db, owner, lock=False)
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


def _document(value: Mapping[str, Any]) -> str:
    return json.dumps(json_safe(value), sort_keys=True)


def _params(row: EpisodeRow, owner: UUID, revision: int, digest: bytes) -> dict[str, Any]:
    return {
        "o": owner,
        "rule_id": row.rule_id,
        "rule_version": row.rule_version,
        "tier": row.tier,
        "severity": row.severity,
        "confidence": row.confidence,
        "data_quality": _document(row.data_quality),
        "metric_keys": list(row.metric_keys),
        "subject": row.subject,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "reference_day": row.reference_day,
        "evidence": _document(row.evidence),
        "r": revision,
        "f": digest,
        "status": row.status,
        "first_seen": day_start(row.first_day),
        "last_seen": day_start(row.last_day),
        "ended_at": None if row.ended_day is None else day_start(row.ended_day),
        "origin": row.origin,
        "notify_state": "pending" if row.origin == "live" else "none",
    }


def content(row: Any) -> tuple[Any, ...]:
    """What a pass compares between a stored row and a computed one (``_params`` or a stored
    row): not the id, ``first_seen``, ``origin``, the notification state or provenance."""
    fields: Mapping[str, Any] = row if isinstance(row, dict) else row._mapping

    def document(value: Any) -> str:
        return canonical(json.loads(value) if isinstance(value, str) else value)

    return (
        fields["rule_version"],
        fields["tier"],
        fields["severity"],
        fields["confidence"],
        tuple(fields["metric_keys"]),
        fields["window_start"],
        fields["window_end"],
        fields["status"],
        fields["last_seen"],
        fields["ended_at"],
        document(fields["data_quality"]),
        document(fields["evidence"]),
    )


@dataclass(slots=True)
class _Diff:
    inserts: list[dict[str, Any]]
    ends: list[dict[str, Any]]  # updates of re-derived episodes that are now ended
    updates: list[dict[str, Any]]
    deletes: list[dict[str, Any]]
    closes: list[dict[str, Any]]  # Q1 episodes ended today (only status and ended_at change)


def _keep(stored: Any, computed: dict[str, Any]) -> dict[str, Any]:
    """The update of a matched episode: its id; ``last_seen`` never before its kept
    ``first_seen``."""
    params = dict(computed, id=stored.id)
    params["last_seen"] = max(params["last_seen"], stored.first_seen)
    return params


def _replayed(
    stored: list[Any], computed: Iterable[EpisodeRow], owner: UUID, r: int, f: bytes, diff: _Diff
) -> None:
    olds: dict[tuple[str, str, date], list[Any]] = defaultdict(list)
    for row in sorted(stored, key=lambda s: (s.first_seen, str(s.id))):
        olds[(row.rule_id, row.subject, row.reference_day)].append(row)
    news: dict[tuple[str, str, date], list[EpisodeRow]] = defaultdict(list)
    for row in computed:
        news[row.match_key].append(row)
    for key in sorted(set(olds) | set(news)):
        old, new = olds.get(key, []), sorted(news.get(key, []), key=lambda e: e.first_day)
        for stored_row, computed_row in zip(old, new, strict=False):
            params = _keep(stored_row, _params(computed_row, owner, r, f))
            if content(stored_row) != content(params):
                (diff.ends if params["status"] == "ended" else diff.updates).append(params)
        diff.deletes.extend({"o": owner, "id": s.id} for s in old[len(new) :])
        diff.inserts.extend(_params(e, owner, r, f) for e in new[len(old) :])


def _q1_row(finding: Finding, keys: tuple[str, ...], today: date) -> EpisodeRow:
    return EpisodeRow(
        rule_id=finding.rule_id,
        rule_version=rule_version(finding.rule_id),
        tier=finding.tier.value,
        severity=finding.severity.value,
        confidence=finding.confidence.value,
        subject=finding.subject,
        metric_keys=keys,
        reference_day=finding.reference_day,
        window_start=finding.window_start,
        window_end=finding.window_end,
        first_day=finding.day,
        last_day=finding.day,
        ended_day=None,
        origin=origin(finding.window_end, today).value,
        evidence=json_safe(finding.evidence),
        data_quality=json_safe(finding.data_quality),
    )


def _q1(
    stored: list[Any], evaluation: Evaluation, today: date, owner: UUID, r: int, f: bytes,
    diff: _Diff,
) -> None:  # fmt: skip
    active = {(s.rule_id, s.subject): s for s in stored if s.status == "active"}
    for result in evaluation.q1:
        old = active.pop((result.rule_id, result.subject), None)
        if result.finding is None:
            if old is not None:
                diff.closes.append(_ended(old, owner, today))
            continue
        row = _q1_row(result.finding, result.metric_keys, today)
        if old is None:
            diff.inserts.append(_params(row, owner, r, f))
            continue
        params = _keep(old, _params(row, owner, r, f))
        # advance_simple: the window grows, the reference day and origin stay.
        params["window_start"] = old.window_start
        params["window_end"] = max(old.window_end, row.window_end)
        if content(old) != content(params):
            diff.updates.append(params)
    for old in active.values():  # a subject no longer reported: the episode ends
        diff.closes.append(_ended(old, owner, today))


def _ended(old: Any, owner: UUID, today: date) -> dict[str, Any]:
    """An active Q1 episode ends today; the rest of the row stays as stored."""
    return {"o": owner, "id": old.id, "ended_at": max(day_start(today), old.last_seen)}


async def _chunks(db: AsyncSession, statement: Any, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), CHUNK):
        await db.execute(statement, rows[start : start + CHUNK])


def diff_rows(
    stored: list[Any], evaluation: Evaluation, today: date, owner: UUID, r: int, f: bytes
) -> _Diff:
    """The statements that turn ``stored`` (this writer's rows) into ``evaluation``."""
    diff = _Diff([], [], [], [], [])
    q1 = set(Q1_RULES)
    _replayed(
        [s for s in stored if s.rule_id not in q1],
        evaluation.episodes,
        owner,
        r,
        f,
        diff,
    )
    _q1([s for s in stored if s.rule_id in q1], evaluation, today, owner, r, f, diff)
    return diff


async def _write(
    db: AsyncSession,
    owner: UUID,
    read: Gathered,
    evaluation: Evaluation,
    *,
    settings: Settings,
    now: datetime,
) -> Outcome:
    await db.execute(_LOCK, {"ns": NORMALISATION, "o": owner})
    if await _revision(db, owner, lock=True) != read.revision:
        return Outcome("stale")
    again = await gather(db, owner, settings=settings, now=now)
    if not isinstance(again, Gathered) or again.fingerprint != read.fingerprint:
        return Outcome("stale")
    stored = list((await db.execute(_STORED, {"o": owner, "rules": list(OWNED_RULES)})).all())
    r, f = read.revision, read.fingerprint
    diff = diff_rows(stored, evaluation, read.inputs.today, owner, r, f)
    await _chunks(db, _DELETE, diff.deletes)
    await _chunks(db, _CLOSE, diff.closes)
    await _chunks(db, _UPDATE, diff.ends)
    await _chunks(db, _UPDATE, diff.updates)
    await _chunks(db, _INSERT, diff.inserts)
    moved = await db.execute(
        _PROVENANCE, {"o": owner, "rules": list(OWNED_RULES), "q1": list(Q1_RULES), "r": r, "f": f}
    )
    ended = len(diff.ends) + len(diff.closes)
    changed = len(diff.inserts) + ended + len(diff.updates) + len(diff.deletes)
    restamped = int(getattr(moved, "rowcount", 0) or 0)
    return Outcome(
        "written" if changed or restamped else "current",
        inserted=len(diff.inserts),
        updated=len(diff.updates),
        ended=ended,
        deleted=len(diff.deletes),
        pending=sum(1 for p in diff.inserts if p["notify_state"] == "pending"),
        evaluated=dict(evaluation.evaluated),
    )


async def evaluate_owner(
    open_tx: OpenTx,
    owner: UUID,
    *,
    settings: Settings,
    now: datetime | None = None,
    between: Callable[[], Awaitable[None]] | None = None,
) -> Outcome:
    """One owner's pass (see the module docstring). ``between`` runs after the read and before
    the write; a job passes its checkpoint, a test interleaves a purge or an ingest."""
    moment = now or datetime.now(UTC)
    async with open_tx() as db:
        read = await gather(db, owner, settings=settings, now=moment)
    if isinstance(read, Outcome):
        return read
    evaluation = evaluate(read.inputs)
    if between is not None:
        await between()
    async with open_tx() as db:
        return await _write(db, owner, read, evaluation, settings=settings, now=moment)


__all__ = [
    "Gathered",
    "Outcome",
    "content",
    "day_start",
    "diff_rows",
    "evaluate_owner",
    "gather",
]
