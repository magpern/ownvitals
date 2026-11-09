"""The ECG inputs of the rule evaluation (#191; ADR 0006 section 11): what E1 to E4 read besides
the observations and daily values ``findings_gather`` reads.

* the **Withings-reported fields** of every ECG recording of the owner (``afib``, the
  ``afib_details`` code, the time and zone), never the signal and never ``estimated``: no finding
  depends on our gate outcome (section 11), and writing ``estimated`` re-evaluates nothing
  (section 15);
* for each recording, the **measure groups A7 links to it now** (``ecg.overlay.a7_links``: same
  connection and timestamp, an ECG type, the same device), the trigger groups of an E1 to E3
  finding; the read function links them again at read time, so a deleted recording is no link;
* the profile's **sex**, for E4's QTc band (unknown: only the 500 ms band).

Runs in ``gather``'s transaction, as the owner under forced RLS; every statement also filters
``owner_id`` (ADR 0002). Nothing here is logged.
"""

from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.analysis.rules.ecg import Recording, Sex
from ownvitals.ecg.overlay import (
    GroupKey,
    RecordingKey,
    a7_links,
    group_device_ids,
    recording_device_id,
)

_RECORDINGS = text(
    "SELECT r.id, r.connection_id, r.signalid, r.recorded_at, r.source_tz, r.model_id, "
    "r.reported, r.afib, r.afib_details FROM ecg_recordings AS r WHERE r.owner_id = :o"
)
_GROUPS = text(
    "SELECT g.grpid, g.connection_id, g.measured_at, g.hash_deviceid, g.model_id, g.raw, "
    "ARRAY(SELECT m.type FROM measures AS m "
    "WHERE m.owner_id = g.owner_id AND m.measure_group_id = g.id) AS types "
    "FROM measure_groups AS g WHERE g.owner_id = :o AND g.deleted_at IS NULL AND EXISTS ("
    "SELECT 1 FROM ecg_recordings AS r WHERE r.owner_id = g.owner_id "
    "AND r.connection_id = g.connection_id AND r.recorded_at = g.measured_at)"
)
_SEX = text("SELECT sex FROM user_profiles WHERE owner_id = :o")


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _code(details: object) -> int | None:
    """``afib_details`` as ingest stores it: ``{"code": n}``."""
    return _int(details.get("code")) if isinstance(details, Mapping) else None  # pyright: ignore[reportUnknownMemberType]


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError, OSError):
        return UTC


def _group(row: Any) -> GroupKey:
    return GroupKey(
        grpid=int(row.grpid),
        connection=row.connection_id,
        measured_at=row.measured_at,
        device_ids=group_device_ids(row.hash_deviceid, row.raw),
        model_id=_int(row.model_id),
        types=frozenset(int(t) for t in row.types or ()),
    )


def _link_key(row: Any) -> RecordingKey:
    """As much of the row as A7 needs; no fingerprint and no ``estimated`` (no gate here)."""
    return RecordingKey(
        id=row.id,
        connection=row.connection_id,
        recorded_at=row.recorded_at,
        device_id=recording_device_id(row.reported),
        model_id=_int(row.model_id),
        fingerprint="",
        estimated=None,
    )


async def read_ecg(
    db: AsyncSession, owner: UUID, *, day_tz: str | None
) -> tuple[tuple[Recording, ...], Sex | None]:
    """The owner's recordings for E1 to E3 (sorted by time) and the profile's sex."""
    rows = (await db.execute(_RECORDINGS, {"o": owner})).all()
    groups = [_group(g) for g in (await db.execute(_GROUPS, {"o": owner})).all()]
    by_moment: dict[tuple[UUID, datetime], list[GroupKey]] = defaultdict(list)
    for g in groups:
        by_moment[(g.connection, g.measured_at)].append(g)
    recordings: list[Recording] = []
    for row in rows:
        key = _link_key(row)
        linked = sorted(
            {g.grpid for g in by_moment[(key.connection, key.recorded_at)] if a7_links(key, g)}
        )
        moment: datetime = row.recorded_at
        recordings.append(
            Recording(
                signalid=int(row.signalid),
                recorded_at=moment.astimezone(UTC),
                day=moment.astimezone(_zone(day_tz or str(row.source_tz))).date(),
                afib=_int(row.afib),
                afib_details=_code(row.afib_details),
                groups=tuple(linked),
            )
        )
    recordings.sort(key=lambda r: r.order)
    sex = (await db.execute(_SEX, {"o": owner})).scalar_one_or_none()
    return tuple(recordings), sex if sex in ("female", "male") else None
