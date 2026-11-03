"""``GET /api/days/{key}/{day}`` and ``GET /api/readings/{id}``: everything recorded on a day and
one reading's drawer (the Reading mock-up; PLAN sections 5 and 7.1 steps 2 and 3).

Read as the signed-in user under forced RLS. A day or a reading that is not the caller's (or does
not exist) answers 404: another user's reading id is indistinguishable from a missing one. The
reading shows its raw value (``value_raw x 10^unit``), ``attrib``, the device's model, the zone it
was taken in, the quality gates and the provenance (``algo_version``, ``input_revision``,
``input_fingerprint``); it carries no Withings id (no group id, no hashed device id).
"""

from datetime import UTC, date
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request

from ownvitals.analysis.quality import EXCLUDING, METRICS, Flag
from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.views.deps import Now
from ownvitals.views.reads import DayReading, reading
from ownvitals.views.schemas import (
    AlsoRecordedOut,
    DayDailyOut,
    DayOut,
    DayReadingOut,
    DeviceOut,
    GateOut,
    RawOut,
    ReadingOut,
    ReadingProvenanceOut,
)

router = APIRouter(prefix="/api")

FIRST_DAY = date(2009, 1, 1)  # the partition floor (withings.streams.FLOOR)
LAST_DAY = date(2100, 1, 1)
_EXCLUDING = frozenset(flag.value for flag in EXCLUDING)


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError, OSError):
        return None


def _local(ts, source_tz: str):
    return ts.astimezone(_zone(source_tz) or UTC)


def _used(flags: tuple[str, ...]) -> bool:
    return not _EXCLUDING.intersection(flags)


def _day_reading(item: DayReading) -> DayReadingOut:
    return DayReadingOut(
        id=item.id,
        at=item.ts,
        local_time=_local(item.ts, item.source_tz).time().isoformat(timespec="seconds"),
        source_tz=item.source_tz,
        value=float(item.value),
        quality_flags=list(item.quality_flags),
        used=_used(item.quality_flags),
    )


@router.get("/days/{key}/{day}", response_model=DayOut)
async def get_day(
    key: str,
    day: date,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
) -> DayOut:
    if key not in METRICS or not FIRST_DAY <= day <= LAST_DAY:
        raise HTTPException(status_code=404)
    async with reading(request.app.state.session_factory, ctx) as r:
        daily = (await r.daily((key,), day, day))[key]
        readings = await r.day_readings(key, day)
        others = await r.day_others(key, day)
    if not daily and not readings:
        raise HTTPException(status_code=404)
    row = daily[0] if daily else None
    return DayOut(
        key=key,
        day=day,
        unit=METRICS[key].unit,
        daily=(
            None
            if row is None
            else DayDailyOut(
                value=float(row.value),
                method=row.method,
                n=row.n,
                minimum=float(row.minimum),
                maximum=float(row.maximum),
                mad=float(row.mad),
                algo_version=row.algo_version,
            )
        ),
        readings=[_day_reading(item) for item in readings],
        also_recorded=[AlsoRecordedOut(metric_key=k, n_readings=n) for k, n in others],
    )


def _gates(flags: tuple[str, ...]) -> list[GateOut]:
    return [
        GateOut(gate="confirmed", passed=Flag.UNCONFIRMED.value not in flags, advisory=False),
        GateOut(gate="plausible", passed=Flag.IMPLAUSIBLE.value not in flags, advisory=False),
        GateOut(gate="not_duplicate", passed=Flag.DUPLICATE.value not in flags, advisory=False),
        GateOut(gate="no_jump", passed=Flag.JUMP.value not in flags, advisory=True),
    ]


@router.get("/readings/{reading_id}", response_model=ReadingOut)
async def get_reading(
    reading_id: UUID,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    now: Now,
) -> ReadingOut:
    del now
    async with reading(request.app.state.session_factory, ctx) as r:
        found = await r.reading(reading_id)
        revision = await r.measures_revision()
    # Only the metrics of the catalog are browsable; another stream's observation is a 404 too.
    if found is None or found.metric_key not in METRICS:
        raise HTTPException(status_code=404)
    spec = METRICS[found.metric_key]
    device = (
        None
        if found.device_model is None and found.device_type is None
        else DeviceOut(
            model_id=found.device_model, type=found.device_type, firmware=found.device_firmware
        )
    )
    return ReadingOut(
        id=found.id,
        metric_key=found.metric_key,
        unit=spec.unit,
        at=found.ts,
        source_at=found.source_ts,
        source_tz=found.source_tz,
        local_day=_local(found.ts, found.source_tz).date(),
        value=float(found.value),
        value_exact=format(found.value, "f"),
        raw=RawOut(
            value_raw=found.value_raw,
            unit_exponent=found.unit_exponent,
            conversion=found.conversion,
        ),
        attrib=found.attrib,
        attrib_key=None if found.attrib is None else f"reading.attrib.{found.attrib}",
        category=found.category,
        device=device,
        quality_flags=list(found.quality_flags),
        gates=_gates(found.quality_flags),
        used_in_daily=_used(found.quality_flags),
        provenance=ReadingProvenanceOut(
            algo_version=found.algo_version,
            input_revision=found.input_revision,
            input_fingerprint=found.input_fingerprint.hex(),
            recompute_pending=found.input_revision != revision,
        ),
    )
