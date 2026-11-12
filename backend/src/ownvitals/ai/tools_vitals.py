"""The ECG, sleep and activity tools (ADR 0014 section 4; #401), run by ``ai.tools.run_tool``
after its checks (enabled, closed arguments) and with its tier-scoped settings.

- **Reads** only through the existing readers, each a ``user_session`` of the caller's
  ``UserContext`` under forced RLS: ``views.ecg_read.list_recordings`` (Withings' own fields; the
  list read never loads the signal), ``views.findings_read.read_ecg_estimates`` (ADR 0006 section
  11's one read of the estimates) and ``views.reads`` (``Reader.stream_days``, the normalised
  sleep and activity values). None writes or takes a user argument.
- **Tier filter**: ``read_ecg_estimates`` loads estimates only with the owner-only switch on, and
  ``run_tool`` hands these handlers settings whose switch is off unless
  ``ai_include_experimental`` is true. Only up-to-date estimates (``estimate_view``). Without one
  the result is ``EcgSummaryOut``, which has no estimates field at all.
- **Days only**: a recording's day is its local day; the recording id stays here (it keys the
  estimates read), and no zone, instant, signal id, device or model leaves.
"""

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai.local_day import local_today, today_of
from ownvitals.ai.tools_vitals_model import (
    ACTIVITY_KEYS,
    ECG_MAX_ITEMS,
    SLEEP_KEYS,
    STREAM_MAX_POINTS,
    ActivityDay,
    ActivitySummaryArgs,
    ActivitySummaryOut,
    EcgClassCount,
    EcgClassification,
    EcgEstimateItem,
    EcgRecordingItem,
    EcgSummaryArgs,
    EcgSummaryExperimentalOut,
    EcgSummaryOut,
    SleepNight,
    SleepSummaryArgs,
    SleepSummaryOut,
)
from ownvitals.config import Settings
from ownvitals.context import UserContext
from ownvitals.ecg.display import AFIB_MEANING
from ownvitals.ecg.overlay import EstimateView
from ownvitals.views.ecg_read import LIST_MAX, Recording, list_recordings
from ownvitals.views.findings_read import read_ecg_estimates
from ownvitals.views.reads import reading

Factory = async_sessionmaker[AsyncSession]
VALUE_DIGITS: Final = 2
_INTERVAL_FIELDS: Final = {
    "qrs_duration": "qrs_duration_ms",
    "pr_interval": "pr_interval_ms",
    "qt_interval": "qt_interval_ms",
    "qtc_interval": "qtc_interval_ms",
}


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), VALUE_DIGITS)


def recording_day(recording: Recording) -> date:
    """The recording's local day (its own zone; UTC when the zone is unknown)."""
    try:
        return recording.recorded_at.astimezone(ZoneInfo(recording.source_tz)).date()
    except (ValueError, KeyError, OSError):
        return recording.recorded_at.astimezone(UTC).date()


def classification(code: int | None) -> EcgClassification:
    if code is None:
        return "not_reported"
    meaning = AFIB_MEANING.get(code)
    if meaning == "negative" or meaning == "positive" or meaning == "inconclusive":
        return meaning
    return "undocumented"


async def _recordings_in(
    factory: Factory, ctx: UserContext, first: date, last: date
) -> list[tuple[date, Recording]]:
    """The user's recordings whose local day is in ``[first, last]``, newest first."""
    found: list[tuple[date, Recording]] = []
    # Newest first: a page whose oldest recording is a day before ``first`` in UTC ends the scan
    # (no zone moves a day by more than one).
    floor = datetime(first.year, first.month, first.day, tzinfo=UTC) - timedelta(days=1)
    offset = 0
    while True:
        page, more = await list_recordings(factory, ctx, limit=LIST_MAX, offset=offset)
        for recording in page:
            day = recording_day(recording)
            if first <= day <= last:
                found.append((day, recording))
        if not more or not page or page[-1].recorded_at < floor:
            return found
        offset += len(page)


def _item(day: date, recording: Recording) -> EcgRecordingItem:
    intervals: dict[str, Any] = dict.fromkeys(_INTERVAL_FIELDS.values())
    for interval in recording.intervals:
        field = _INTERVAL_FIELDS.get(interval.key)
        if field is not None:
            intervals[field] = _round(interval.value)
    return EcgRecordingItem(
        day=day,
        classification=classification(recording.afib),
        heart_rate_bpm=recording.heart_rate,
        **intervals,
    )


def _number(hrv: Any, key: str) -> float | None:
    value = None if hrv is None else hrv.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return _round(float(value))


def _estimate(day: date, view: EstimateView) -> EcgEstimateItem | None:
    item = EcgEstimateItem(
        day=day,
        heart_rate_bpm=_round(view.hr_bpm),
        rmssd_ms=_number(view.hrv, "rmssd_ms"),
        sdnn_ms=_number(view.hrv, "sdnn_ms"),
    )
    if item.heart_rate_bpm is None and item.rmssd_ms is None and item.sdnn_ms is None:
        return None
    return item


async def ecg_summary(
    factory: Factory, ctx: UserContext, settings: Settings, args: EcgSummaryArgs, now: datetime
) -> EcgSummaryOut:
    last = await today_of(factory, ctx, now)  # the user's local day (#443), as recordings' days
    first = last - timedelta(days=args.period - 1)
    found = await _recordings_in(factory, ctx, first, last)
    counts: Counter[EcgClassification] = Counter(classification(r.afib) for _, r in found)
    shown = found[:ECG_MAX_ITEMS]
    base: dict[str, Any] = {
        "period_days": args.period,
        "recordings": len(found),
        "classifications": [
            EcgClassCount(classification=c, count=n) for c, n in sorted(counts.items())
        ],
        "items": [_item(day, r) for day, r in shown],
        "truncated": len(found) > ECG_MAX_ITEMS,
    }
    views = await read_ecg_estimates(factory, ctx, settings, [r.id for _, r in shown])
    estimates = [
        e for day, r in shown if r.id in views and (e := _estimate(day, views[r.id])) is not None
    ]
    if not estimates:  # absent, not empty: the model is not told estimates exist
        return EcgSummaryOut(**base)
    return EcgSummaryExperimentalOut(**base, estimates=estimates)


async def _per_day(
    factory: Factory, ctx: UserContext, keys: dict[str, str], period: int, now: datetime
) -> list[dict[str, Any]]:
    """One row per day with data in the period, oldest first: ``{"day": d, field: value}``."""
    async with reading(factory, ctx) as r:
        last = local_today(now, await r.home_zone())
        first = last - timedelta(days=period - 1)
        found = await r.stream_days(tuple(sorted(keys.values())), first, last)
    days = sorted({d for values in found.values() for d in values})[-STREAM_MAX_POINTS:]
    return [
        {"day": d, **{field: _round(found.get(key, {}).get(d)) for field, key in keys.items()}}
        for d in days
    ]


async def sleep_summary(
    factory: Factory, ctx: UserContext, settings: Settings, args: SleepSummaryArgs, now: datetime
) -> SleepSummaryOut:
    rows = await _per_day(factory, ctx, dict(SLEEP_KEYS), args.period, now)
    nights = [SleepNight(**row) for row in rows]
    return SleepSummaryOut(period_days=args.period, nights_with_data=len(nights), nights=nights)


async def activity_summary(
    factory: Factory,
    ctx: UserContext,
    settings: Settings,
    args: ActivitySummaryArgs,
    now: datetime,
) -> ActivitySummaryOut:
    rows = await _per_day(factory, ctx, dict(ACTIVITY_KEYS), args.period, now)
    days = [ActivityDay(**row) for row in rows]
    return ActivitySummaryOut(period_days=args.period, days_with_data=len(days), days=days)
