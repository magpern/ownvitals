"""The database reads of the read API (PLAN section 8): ``/api/status``, ``/api/metrics``,
``/api/days`` and ``/api/readings``, ``/api/nerve/summary``.

``reading()`` opens one ``user_session`` (forced RLS) and hands out a ``Reader``; every statement
also filters ``owner_id`` itself (ADR 0002). This is the one module of ``ownvitals.views`` besides
``findings_read`` that opens a session (``tests/session_scan.py``). It never reads ``findings``,
``estimated`` or ``ecg_recordings``: findings come only through ``views.findings_read``
(``tests/test_findings_read_guard.py``).

Nothing here returns a Withings identifier: ``source_grpid``, the hashed device id and the
connection id stay in this module's SQL.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.repository import list_connections
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.views.catalog import STATUS_STREAMS
from ownvitals.views.read_records import DailyRecord, DayReading, ReadingDetail, StatusFacts
from ownvitals.views.states import ConnectionFacts, StreamFacts
from ownvitals.withings.streams import stream_named, windows

REVISION_STREAM = "measures"  # the counter every metric_daily row is stamped with
ROWS_LIMIT = 500  # readings of one metric on one day

_CURSORS = text(
    "SELECT stream, capability, backfill_state, last_ok_at, last_error_category "
    "FROM sync_cursors WHERE owner_id = :o AND connection_id = :c"
)
_ACTIVE_IMPORTS = text(
    "SELECT DISTINCT input->>'stream' AS stream FROM jobs WHERE owner_id = :o "
    "AND kind = 'withings_backfill' AND state IN ('queued', 'running')"
)
_LAST_READING = text(
    "SELECT max(measured_at) FROM measure_groups WHERE owner_id = :o AND deleted_at IS NULL"
)
_REVISION = text("SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s")
_DAILY = text(
    "SELECT metric_key, day, value, method, n, min, max, mad, algo_version, input_revision "
    "FROM metric_daily WHERE owner_id = :o AND metric_key = ANY(CAST(:keys AS text[])) "
    "AND day >= :lo AND day <= :hi ORDER BY metric_key, day"
)
_UNCONFIRMED = text(
    "SELECT CAST(ts AT TIME ZONE source_tz AS date) AS day, count(*) AS n FROM observations "
    "WHERE owner_id = :o AND metric_key = :k AND ts >= :lo AND ts < :hi "
    "AND 'unconfirmed' = ANY(quality_flags) "
    "AND CAST(ts AT TIME ZONE source_tz AS date) BETWEEN :first AND :last "
    "GROUP BY 1 ORDER BY 1"
)
_DAY_READINGS = text(
    "SELECT id, ts, source_tz, value, quality_flags FROM observations "
    "WHERE owner_id = :o AND metric_key = :k AND ts >= :lo AND ts < :hi "
    "AND CAST(ts AT TIME ZONE source_tz AS date) = :day ORDER BY ts, id LIMIT :limit"
)
_DAY_OTHERS = text(
    "SELECT metric_key, count(*) AS n FROM observations WHERE owner_id = :o AND metric_key <> :k "
    "AND ts >= :lo AND ts < :hi AND CAST(ts AT TIME ZONE source_tz AS date) = :day "
    "GROUP BY metric_key ORDER BY metric_key"
)
_READING = text(
    "SELECT o.id, o.metric_key, o.ts, o.source_ts, o.source_tz, o.value, o.source_value_raw, "
    "o.source_unit, o.conversion, o.quality_flags, o.algo_version, o.input_revision, "
    "o.input_fingerprint, g.attrib, g.category, d.model_id, d.type AS device_type, d.fw "
    "FROM observations AS o "
    "LEFT JOIN LATERAL (SELECT attrib, category, connection_id, hash_deviceid, model_id "
    "FROM measure_groups AS m WHERE m.owner_id = o.owner_id AND m.grpid = o.source_grpid "
    "AND m.deleted_at IS NULL ORDER BY m.modified_at DESC NULLS LAST, m.id LIMIT 1) AS g ON true "
    "LEFT JOIN devices AS d ON d.owner_id = o.owner_id AND d.connection_id = g.connection_id "
    "AND d.hash_deviceid = g.hash_deviceid "
    "WHERE o.owner_id = :o AND o.id = :id"
)


def _state(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _around(day: date) -> tuple[datetime, datetime]:
    """UTC bounds that hold every instant whose local date (any zone, UTC-12 to UTC+14) is
    ``day``; they let the partitioned ``observations`` prune. The local date is checked in SQL."""
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start - timedelta(days=1), start + timedelta(days=2)


class Reader:
    """The reads of one signed-in user inside one transaction."""

    def __init__(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], ctx: UserContext
    ) -> None:
        self._db = db
        self._factory = factory
        self._ctx = ctx
        self._owner = ctx.user_id

    async def status_facts(self, now: datetime) -> StatusFacts:
        last = (await self._db.execute(_LAST_READING, {"o": self._owner})).scalar_one_or_none()
        # The connection row is read through its repository (ADR 0004 section 5): only
        # ``ownvitals.connections`` holds SQL on that table. It is the user's newest live row.
        live = [
            c
            for c in await list_connections(self._factory, self._ctx)
            if c.provider == "withings" and c.status != "disconnected"
        ]
        if not live:
            return StatusFacts(None, last_reading_at=last)
        row = live[-1]
        connection = ConnectionFacts(status=row.status, last_error=row.last_error)
        active = {
            r.stream
            for r in (await self._db.execute(_ACTIVE_IMPORTS, {"o": self._owner})).all()
            if r.stream
        }
        cursors = {
            r.stream: r
            for r in (await self._db.execute(_CURSORS, {"o": self._owner, "c": row.id})).all()
        }
        streams: dict[str, StreamFacts] = {}
        for name in STATUS_STREAMS:
            cursor = cursors.get(name)
            if cursor is None:
                continue
            state = _state(cursor.backfill_state)
            demand = state.get("demand_days")
            total = len(
                windows(stream_named(name), now, demand_days=demand if _int(demand) else None)
            )
            streams[name] = StreamFacts(
                stream=name,
                capability=cursor.capability,
                backfill_status=state.get("status") if state.get("status") else None,
                windows_done=_int(state.get("index")),
                windows_total=total,
                import_job_active=name in active,
                last_ok_at=cursor.last_ok_at,
                last_error_category=cursor.last_error_category,
                skipped_out_of_range=_int(state.get("skipped_out_of_range")),
            )
        synced = [c.last_ok_at for c in cursors.values() if c.last_ok_at is not None]
        return StatusFacts(
            connection, streams, last_reading_at=last, last_sync_at=max(synced, default=None)
        )

    async def measures_revision(self) -> int:
        found = (
            await self._db.execute(_REVISION, {"o": self._owner, "s": REVISION_STREAM})
        ).scalar_one_or_none()
        return 0 if found is None else int(found)

    async def daily(
        self, keys: tuple[str, ...], lo: date = date(2009, 1, 1), hi: date = date(2100, 1, 1)
    ) -> dict[str, list[DailyRecord]]:
        rows = (
            await self._db.execute(
                _DAILY, {"o": self._owner, "keys": list(keys), "lo": lo, "hi": hi}
            )
        ).all()
        found: dict[str, list[DailyRecord]] = {key: [] for key in keys}
        for r in rows:
            found[r.metric_key].append(
                DailyRecord(
                    r.metric_key,
                    r.day,
                    r.value,
                    r.method,
                    r.n,
                    r.min,
                    r.max,
                    r.mad,
                    r.algo_version,
                    int(r.input_revision),
                )
            )
        return found

    async def unconfirmed_days(self, key: str, first: date, last: date) -> dict[date, int]:
        lo, _ = _around(first)
        _, hi = _around(last)
        rows = (
            await self._db.execute(
                _UNCONFIRMED,
                {"o": self._owner, "k": key, "lo": lo, "hi": hi, "first": first, "last": last},
            )
        ).all()
        return {r.day: int(r.n) for r in rows}

    async def day_readings(self, key: str, day: date) -> list[DayReading]:
        lo, hi = _around(day)
        rows = (
            await self._db.execute(
                _DAY_READINGS,
                {"o": self._owner, "k": key, "lo": lo, "hi": hi, "day": day, "limit": ROWS_LIMIT},
            )
        ).all()
        return [DayReading(r.id, r.ts, r.source_tz, r.value, tuple(r.quality_flags)) for r in rows]

    async def day_others(self, key: str, day: date) -> list[tuple[str, int]]:
        lo, hi = _around(day)
        rows = (
            await self._db.execute(
                _DAY_OTHERS, {"o": self._owner, "k": key, "lo": lo, "hi": hi, "day": day}
            )
        ).all()
        return [(r.metric_key, int(r.n)) for r in rows]

    async def reading(self, reading_id: UUID) -> ReadingDetail | None:
        r = (await self._db.execute(_READING, {"o": self._owner, "id": reading_id})).first()
        if r is None:
            return None
        return ReadingDetail(
            id=r.id,
            metric_key=r.metric_key,
            ts=r.ts,
            source_ts=r.source_ts,
            source_tz=r.source_tz,
            value=r.value,
            value_raw=int(r.source_value_raw),
            unit_exponent=int(r.source_unit),
            conversion=r.conversion,
            quality_flags=tuple(r.quality_flags),
            algo_version=r.algo_version,
            input_revision=int(r.input_revision),
            input_fingerprint=bytes(r.input_fingerprint),
            attrib=r.attrib,
            category=r.category,
            device_model=r.model_id,
            device_type=r.device_type,
            device_firmware=r.fw,
        )


@asynccontextmanager
async def reading(
    factory: async_sessionmaker[AsyncSession], ctx: UserContext
) -> AsyncIterator[Reader]:
    async with user_session(factory, ctx) as db:
        yield Reader(db, factory, ctx)
