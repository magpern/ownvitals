"""Pure assembly of the metric views from stored daily values (PLAN section 7.1 steps 3 to 5,
section 8): the data state, the typical range, the sparkline, the trend and the change points.
No database, clock or Withings: records and the day "today" come in, response models go out.

All statistics are the merged analysis library's, called as the finding rules call them
(``analysis.rules.core.display_band``, ``methods.trend``, ``methods.changepoint``), so the page
never shows a number the rules would not agree with. A series below its minimum-data policy gets
``insufficient_data`` with its progress, never a trend, a band or change points.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from typing import Final

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.policy import InsufficientData, Sufficient
from ownvitals.analysis.quality import METRICS
from ownvitals.analysis.rules.core import Band, display_band
from ownvitals.views import states
from ownvitals.views.catalog import (
    BAND_DAYS,
    SPARKLINE_DAYS,
    STREAM_OF,
    area_of,
)
from ownvitals.views.reads import DailyRecord
from ownvitals.views.schemas import (
    BandOut,
    LatestOut,
    MetricTileOut,
    ProgressOut,
    SparkPoint,
)

NEVER_SHOWN: Final = frozenset(states.BLOCKED)


def to_daily(records: Sequence[DailyRecord]) -> list[DailyValue]:
    return [
        DailyValue(
            day=r.day,
            value=r.value,
            method=DailyMethod(r.method),
            n=r.n,
            minimum=r.minimum,
            maximum=r.maximum,
            mad=r.mad,
            algo_version=r.algo_version,
        )
        for r in records
    ]


def anchor_day(values: Sequence[DailyValue], today: date) -> date:
    """The window's last day: today, or a later day a time zone ahead of UTC already recorded."""
    return max(today, values[-1].day) if values else today


def progress_of(verdict: InsufficientData, window_days: int) -> ProgressOut:
    p = states.progress_toward(
        verdict.requirement,
        verdict.shortfalls,
        n_days=verdict.data_quality.n_days,
        span_days=verdict.data_quality.span_days,
        window_days=window_days,
    )
    return ProgressOut(
        days_with_data=p.days_with_data,
        days_required=p.days_required,
        span_days=p.span_days,
        span_required=p.span_required,
        shortfalls=list(p.shortfalls),
        fraction=p.fraction,
    )


def band_out(band: Band, as_of: date) -> BandOut:
    return BandOut(
        median=band.median,
        low=band.low,
        high=band.high,
        half_width=band.half_width,
        k=band.k,
        window_days=BAND_DAYS,
        as_of=as_of,
    )


def latest_of(records: Sequence[DailyRecord]) -> LatestOut | None:
    if not records:
        return None
    last = records[-1]
    return LatestOut(day=last.day, value=float(last.value), n=last.n, method=last.method)


def recompute_pending(records: Sequence[DailyRecord], revision: int) -> bool:
    """A daily row is stale when the stream's counter moved past the one it was computed from
    (PLAN section 5); the page says "updating", the value is still the last good one."""
    return any(r.input_revision != revision for r in records)


def sparkline(values: Sequence[DailyValue], anchor: date) -> list[SparkPoint]:
    if not values:
        return []
    by_day = {v.day: float(v.value) for v in values}
    first = anchor - timedelta(days=SPARKLINE_DAYS - 1)
    return [
        SparkPoint(day=first + timedelta(days=i), value=by_day.get(first + timedelta(days=i)))
        for i in range(SPARKLINE_DAYS)
    ]


def metric_state(
    stream: states.StreamState,
    band: Band | Sufficient | InsufficientData,
    *,
    pending: bool,
    window_days: int = BAND_DAYS,
) -> tuple[str, ProgressOut | None]:
    """The metric's state: the stream's, then ``insufficient_data`` below the baseline policy
    (the typical range needs it), then ``stale`` while a recompute is pending."""
    if stream.state in NEVER_SHOWN or stream.state in {states.IMPORTING, states.INCOMPLETE}:
        return stream.state, None
    if isinstance(band, InsufficientData):
        return states.INSUFFICIENT, progress_of(band, window_days)
    if pending and stream.state == states.UP_TO_DATE:
        return states.STALE, None
    return stream.state, None


def tile(
    key: str,
    records: Sequence[DailyRecord],
    stream: states.StreamState,
    *,
    today: date,
    revision: int,
) -> MetricTileOut:
    values = to_daily(records)
    anchor = anchor_day(values, today)
    band = display_band(key, values, anchor)
    state, progress = metric_state(stream, band, pending=recompute_pending(records, revision))
    return MetricTileOut(
        key=key,
        area=area_of(key),
        unit=METRICS[key].unit,
        stream=STREAM_OF[key],
        state=state,
        progress_percent=stream.progress_percent,
        progress=progress,
        latest=latest_of(records),
        typical_range=None if isinstance(band, InsufficientData) else band_out(band, anchor),
        sparkline=sparkline(values, anchor),
    )
