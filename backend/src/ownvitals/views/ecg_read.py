"""The ECG screen's reads of the user's stored recordings (ADR 0006 section 3, PLAN 8).

One of the three modules of ``ownvitals.views`` that open a ``user_session`` (with ``reads`` and
``findings_read``; ``tests/session_scan.py``). It reads Withings' own values and the stored
signal of the signed-in user's recordings under forced RLS, and every statement filters
``owner_id`` too (ADR 0002). It never reads our derived column of that table: our estimates come
only through ``views.findings_read.read_ecg_estimates`` (section 11), and the PDF never asks for
them.

The signal is decoded here, never logged and never part of an error. A recording whose signal is
missing or does not decode has ``leads=None``; nothing is repaired or guessed.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.ecg.display import INTERVALS, interval_value
from ownvitals.ingest.codec import decode_signal

LIST_DEFAULT: Final = 50
LIST_MAX: Final = 200
_LOG = logging.getLogger(__name__)

_COLUMNS = (
    "r.id, r.recorded_at, r.source_tz, r.model_id, r.afib, r.afib_details, r.heart_rate, "
    "r.sampling_hz, r.wearposition"
)
_LIST = text(
    f"SELECT {_COLUMNS}, r.signal IS NOT NULL AS has_signal "  # noqa: S608 - constant columns
    "FROM ecg_recordings AS r WHERE r.owner_id = :owner "
    "ORDER BY r.recorded_at DESC, r.id LIMIT :limit OFFSET :offset"
)
_ONE = text(
    f"SELECT {_COLUMNS}, r.signal FROM ecg_recordings AS r "  # noqa: S608 - constant columns
    "WHERE r.owner_id = :owner AND r.id = :id"
)
# ADR 0006 A7: the intervals belong to the recording whose group has the same connection and
# time. Several groups at one time are possible; the lowest grpid of a type wins.
_INTERVALS = text(
    "SELECT DISTINCT ON (r.id, m.type) r.id AS recording_id, m.type, m.value_raw, m.unit "
    "FROM ecg_recordings AS r "
    "JOIN measure_groups AS g ON g.owner_id = r.owner_id AND g.connection_id = r.connection_id "
    "AND g.measured_at = r.recorded_at AND g.deleted_at IS NULL "
    "JOIN measures AS m ON m.owner_id = g.owner_id AND m.measure_group_id = g.id "
    "AND m.type = ANY(CAST(:types AS integer[])) "
    "WHERE r.owner_id = :owner AND r.id = ANY(CAST(:ids AS uuid[])) "
    "ORDER BY r.id, m.type, g.grpid"
)

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Interval:
    """One interval Withings reported with the recording (measure type 135 to 138)."""

    key: str
    value: float


@dataclass(frozen=True)
class Recording:
    """Withings' own fields of one recording. ``repr`` is fixed: no values."""

    id: UUID
    recorded_at: datetime
    source_tz: str
    model_id: int | None
    afib: int | None
    afib_details: int | None
    heart_rate: int | None
    sampling_hz: int | None
    wearposition: int | None
    intervals: tuple[Interval, ...]
    has_signal: bool
    leads: tuple[tuple[int, ...], ...] | None = None  # int32 microvolts, set on a detail read

    def __repr__(self) -> str:
        return "Recording redacted"


def _code(row: Any, name: str) -> int | None:
    """``afib_details`` is stored as a JSON object or number; only an integer code is shown."""
    value = row[name]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        inner = value.get("code")  # pyright: ignore[reportUnknownMemberType]
        return inner if isinstance(inner, int) and not isinstance(inner, bool) else None
    return None


def _decode(frame: object) -> tuple[tuple[int, ...], ...] | None:
    if frame is None:
        return None
    try:
        leads = decode_signal(bytes(frame))  # pyright: ignore[reportArgumentType]
    except Exception as exc:  # a frame that does not decode is "no signal"
        _LOG.warning("ecg signal not decodable: %s", type(exc).__name__)  # the class only
        return None
    return tuple(tuple(lead) for lead in leads)


def _recording(
    row: Any, intervals: Sequence[Interval], *, has_signal: bool, leads: Any = None
) -> Recording:
    return Recording(
        id=row["id"],
        recorded_at=row["recorded_at"],
        source_tz=row["source_tz"],
        model_id=row["model_id"],
        afib=row["afib"],
        afib_details=_code(row, "afib_details"),
        heart_rate=row["heart_rate"],
        sampling_hz=row["sampling_hz"],
        wearposition=row["wearposition"],
        intervals=tuple(intervals),
        has_signal=has_signal,
        leads=leads,
    )


async def _intervals(
    db: AsyncSession, owner: UUID, ids: Sequence[UUID]
) -> dict[UUID, list[Interval]]:
    if not ids:
        return {}
    params = {"owner": owner, "ids": list(ids), "types": sorted(INTERVALS)}
    found: dict[UUID, list[Interval]] = {}
    for row in (await db.execute(_INTERVALS, params)).mappings():
        key = INTERVALS[row["type"]][0]
        found.setdefault(row["recording_id"], []).append(
            Interval(key=key, value=interval_value(int(row["value_raw"]), int(row["unit"])))
        )
    return found


async def list_recordings(
    factory: SessionFactory, ctx: UserContext, *, limit: int = LIST_DEFAULT, offset: int = 0
) -> tuple[list[Recording], bool]:
    """The user's recordings, newest first, and whether another page follows."""
    async with user_session(factory, ctx) as db:
        params = {"owner": ctx.user_id, "limit": limit + 1, "offset": offset}
        rows = list((await db.execute(_LIST, params)).mappings())
        more = len(rows) > limit
        rows = rows[:limit]
        linked = await _intervals(db, ctx.user_id, [row["id"] for row in rows])
    return (
        [
            _recording(row, linked.get(row["id"], ()), has_signal=bool(row["has_signal"]))
            for row in rows
        ],
        more,
    )


async def read_recording(
    factory: SessionFactory, ctx: UserContext, recording_id: UUID
) -> Recording | None:
    """One recording with its decoded signal; ``None`` when it is not the user's (RLS)."""
    async with user_session(factory, ctx) as db:
        row = (
            (await db.execute(_ONE, {"owner": ctx.user_id, "id": recording_id})).mappings().first()
        )
        if row is None:
            return None
        linked = await _intervals(db, ctx.user_id, [recording_id])
    leads = _decode(row["signal"])
    return _recording(row, linked.get(recording_id, ()), has_signal=leads is not None, leads=leads)


__all__ = [
    "LIST_DEFAULT",
    "LIST_MAX",
    "Interval",
    "Recording",
    "list_recordings",
    "read_recording",
]
