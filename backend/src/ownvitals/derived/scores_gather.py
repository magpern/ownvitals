"""The read side of the scores pass (#188): everything one pass reads, in one transaction.

Every statement filters ``owner_id`` itself and runs under forced RLS. The three stream revisions
(``measures``, ``sleep_summary``, ``activity_daily``) are read first; ``input_revision`` of a
written row is their digest (``scores_revision``, ``docs/dev/derived.md``). ``behind`` when a
daily row or a sleep or activity observation carries
another revision than the stream's (the normaliser or the daily pass has not caught up).
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.config import Settings
from ownvitals.derived.daily_db import read_day_tz
from ownvitals.derived.findings_gather import today_in_zone
from ownvitals.derived.scores_compute import (
    DAILY_KEYS,
    OBSERVATION_KEYS,
    Points,
    ScoreInputs,
    fingerprint,
)
from ownvitals.derived.scores_revision import revision_digest
from ownvitals.derived.scores_tables import TableState
from ownvitals.flags import effective

STREAMS: Final = ("measures", "sleep_summary", "activity_daily")

_REVISION = text("SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s")
_REVISION_LOCK = text(
    "SELECT revision FROM data_revisions WHERE owner_id = :o AND stream = :s FOR SHARE"
)
_DAILY = text(
    "SELECT metric_key, day, method, value, n, min, max, mad FROM metric_daily "
    "WHERE owner_id = :o AND metric_key = ANY(CAST(:keys AS text[]))"
)
_BEHIND_DAILY = text(
    "SELECT count(*) FROM metric_daily WHERE owner_id = :o "
    "AND metric_key = ANY(CAST(:keys AS text[])) AND input_revision <> :r"
)
_POINTS = text(
    "SELECT metric_key, ts, source_tz, value FROM observations WHERE owner_id = :o "
    "AND metric_key = ANY(CAST(:keys AS text[])) AND source_type IS NULL "
    "AND NOT (quality_flags && ARRAY['unconfirmed','implausible','duplicate'])"
)
_BEHIND_POINTS = text(
    "SELECT count(*) FROM observations WHERE owner_id = :o AND source_type IS NULL "
    "AND metric_key LIKE :pattern AND input_revision <> :r"
)
_LATEST_ZONE = text(
    "SELECT source_tz FROM observations WHERE owner_id = :o "
    "AND NOT (quality_flags && ARRAY['unconfirmed','implausible','duplicate']) "
    "ORDER BY ts DESC, source_grpid DESC NULLS LAST LIMIT 1"
)
_PROFILE = text("SELECT birth_year, sex FROM user_profiles WHERE owner_id = :o")


@dataclass(frozen=True, slots=True)
class Gathered:
    revisions: tuple[int, int, int]
    fingerprint: bytes
    inputs: ScoreInputs

    @property
    def input_revision(self) -> int:
        return revision_digest(self.revisions)


@dataclass(frozen=True, slots=True)
class Skipped:
    """``nothing`` (no revision row of any stream) or ``behind``."""

    status: str


async def read_revisions(db: AsyncSession, owner: UUID, *, lock: bool) -> tuple[int, int, int]:
    statement = _REVISION_LOCK if lock else _REVISION
    out = []
    for stream in STREAMS:
        row = (await db.execute(statement, {"o": owner, "s": stream})).first()
        out.append(0 if row is None else int(row.revision))
    return (out[0], out[1], out[2])


def _local_day(ts: datetime, zone: str, day_tz: str | None) -> date:
    try:
        return ts.astimezone(ZoneInfo(day_tz or zone)).date()
    except (ValueError, KeyError, OSError):
        return ts.astimezone(UTC).date()


def _daily(rows: Any) -> dict[str, tuple[DailyValue, ...]]:
    out: dict[str, list[DailyValue]] = defaultdict(list)
    for r in rows:
        out[r.metric_key].append(
            DailyValue(r.day, r.value, DailyMethod(r.method), int(r.n), r.min, r.max, r.mad)
        )
    return {k: tuple(sorted(v, key=lambda d: d.day)) for k, v in out.items()}


def _points(rows: Any, day_tz: str | None) -> dict[str, Points]:
    out: dict[str, dict[date, float]] = defaultdict(dict)
    for r in sorted(rows, key=lambda r: (r.metric_key, r.ts)):
        out[r.metric_key][_local_day(r.ts, str(r.source_tz), day_tz)] = float(r.value)
    return {k: tuple(sorted(v.items())) for k, v in out.items()}


async def gather(
    db: AsyncSession,
    owner: UUID,
    *,
    settings: Settings,
    table: TableState,
    now: datetime,
) -> Gathered | Skipped:
    revisions = await read_revisions(db, owner, lock=False)
    if not any(revisions):
        return Skipped("nothing")
    keys = list(DAILY_KEYS)
    stale = (
        await db.execute(_BEHIND_DAILY, {"o": owner, "keys": keys, "r": revisions[0]})
    ).scalar_one()
    for pattern, revision in (("sleep\\_%", revisions[1]), ("activity\\_%", revisions[2])):
        stale += (
            await db.execute(_BEHIND_POINTS, {"o": owner, "pattern": pattern, "r": revision})
        ).scalar_one()
    if stale:
        return Skipped("behind")
    day_tz = await read_day_tz(db, owner)
    profile = (await db.execute(_PROFILE, {"o": owner})).first()
    experimental = await effective.experimental_findings(db, settings)
    daily = _daily((await db.execute(_DAILY, {"o": owner, "keys": keys})).all())
    points = _points(
        (await db.execute(_POINTS, {"o": owner, "keys": list(OBSERVATION_KEYS)})).all(), day_tz
    )
    latest = (await db.execute(_LATEST_ZONE, {"o": owner})).first()
    inputs = ScoreInputs(
        day=today_in_zone(now, day_tz, None if latest is None else str(latest.source_tz)),
        daily=daily,
        points=points,
        birth_year=None if profile is None else profile.birth_year,
        sex=None if profile is None else profile.sex,
        table=table.table,
        table_error=table.error,
        table_digest=table.digest,
        experimental=experimental,
        day_tz=day_tz,
    )
    return Gathered(revisions, fingerprint(inputs, revisions), inputs)


__all__ = ["Gathered", "Skipped", "gather", "read_revisions"]
