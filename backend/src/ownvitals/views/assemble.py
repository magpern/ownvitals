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
from ownvitals.analysis.dataquality import summarise
from ownvitals.analysis.methods.changepoint import detect_changes
from ownvitals.analysis.methods.series import EPOCH, Series
from ownvitals.analysis.methods.trend import DEFAULT_ALPHA, MAX_POINTS, trend
from ownvitals.analysis.policy import InsufficientData, Purpose, Sufficient, evaluate_metric
from ownvitals.analysis.quality import METRICS
from ownvitals.analysis.rules.core import DISPLAY_K, Band, display_band, min_half_width
from ownvitals.views import states
from ownvitals.views.catalog import (
    BAND_DAYS,
    RANGE_DAYS,
    SPARKLINE_DAYS,
    STREAM_OF,
    area_of,
)
from ownvitals.views.reads import DailyRecord
from ownvitals.views.schemas import (
    BandOut,
    ChangePointOut,
    ChangePointsOut,
    LatestOut,
    MethodOut,
    MetricDetailOut,
    MetricTileOut,
    ProgressOut,
    ProvenanceOut,
    SeriesPoint,
    SparkPoint,
    TrendOut,
    UnconfirmedDay,
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


def window_for(range_name: str, values: Sequence[DailyValue], anchor: date) -> tuple[date, int]:
    """First day and length of the chart window; ``all`` starts at the first day with data."""
    if range_name == "all":
        days = (anchor - values[0].day).days + 1 if values else 1
    else:
        days = RANGE_DAYS[range_name]
    return anchor - timedelta(days=days - 1), days


def _series(inside: Sequence[DailyValue], start: date, days: int) -> list[SeriesPoint]:
    by_day = {v.day: v for v in inside}
    points: list[SeriesPoint] = []
    for i in range(days):
        day = start + timedelta(days=i)
        v = by_day.get(day)
        points.append(
            SeriesPoint(day=day, value=None, n=0, method=None, minimum=None, maximum=None)
            if v is None
            else SeriesPoint(
                day=day,
                value=float(v.value),
                n=v.n,
                method=v.method.value,
                minimum=float(v.minimum),
                maximum=float(v.maximum),
            )
        )
    return points


def _trend(key: str, inside: Sequence[DailyValue], anchor: date, days: int) -> TrendOut:
    verdict = evaluate_metric(key, Purpose.TREND, inside, window_end=anchor, window_days=days)
    if isinstance(verdict, InsufficientData):
        return TrendOut(state=states.INSUFFICIENT, progress=progress_of(verdict, days))
    # The method holds the pairwise slopes in memory: at most MAX_POINTS days, the latest ones.
    used = list(inside)[-MAX_POINTS:]
    result = trend(Series.from_daily(used, window_days=days))
    return TrendOut(
        state="ok",
        direction=result.direction.value,
        significant=result.significant,
        slope_per_day=result.slope,
        slope_low=result.slope_low,
        slope_high=result.slope_high,
        p_value=result.p_value,
        alpha=result.alpha,
        n_days=result.inputs.n_days,
        correction=result.correction.value,
        algo_version=result.algo_version,
    )


def _change_points(
    key: str, inside: Sequence[DailyValue], anchor: date, days: int
) -> ChangePointsOut:
    found = detect_changes(inside, metric_key=key, window_end=anchor, window_days=days)
    if isinstance(found, InsufficientData):
        return ChangePointsOut(state=states.INSUFFICIENT, progress=progress_of(found, days))
    return ChangePointsOut(
        state="ok",
        searched=found.searched,
        points=[
            ChangePointOut(
                day=EPOCH + timedelta(days=round(cp.time)),
                previous_day=EPOCH + timedelta(days=round(cp.previous_time)),
                mean_before=cp.mean_before,
                mean_after=cp.mean_after,
                shift=cp.shift,
                shift_sigmas=cp.shift_sigmas,
            )
            for cp in found.change_points
        ],
        algo_version=found.algo_version,
    )


def method_of(key: str, trend_out: TrendOut) -> MethodOut:
    """How the numbers were made, in words and in the parameters behind them."""
    return MethodOut(
        text=(
            "Each day's value is the first reading between 04:00 and 11:00 local time, otherwise "
            "the median of the day's readings. Readings marked unconfirmed, implausible or "
            "duplicate are shown but not used. The typical range is the median plus or minus "
            f"{DISPLAY_K} times the scaled median absolute deviation of the last {BAND_DAYS} "
            "days, never narrower than a minimum half-width. The trend is a Theil-Sen slope with "
            "a Mann-Kendall test corrected for autocorrelation. Change points are searched with "
            "PELT, only on a series with enough data. A day without a reading is a gap, not zero."
        ),
        parameters={
            "morning_window": "04:00-11:00 local",
            "band_k": DISPLAY_K,
            "band_window_days": BAND_DAYS,
            "band_min_half_width": min_half_width(key),
            "trend_alpha": DEFAULT_ALPHA,
            "trend_correction": trend_out.correction or "ar1",
        },
        algo_versions={
            "daily": "daily/1",
            "trend": trend_out.algo_version or "trend",
            "band": "rules/1",
        },
    )


def detail(
    key: str,
    records: Sequence[DailyRecord],
    stream: states.StreamState,
    unconfirmed: dict[date, int] | None,
    *,
    range_name: str,
    start: date,
    days: int,
    anchor: date,
    revision: int,
) -> MetricDetailOut:
    values = to_daily(records)
    inside = [v for v in values if start <= v.day <= anchor]
    band = display_band(key, values, anchor)
    state, _ = metric_state(stream, band, pending=recompute_pending(records, revision))
    trend_out = _trend(key, inside, anchor, days)
    shown = [r for r in records if start <= r.day <= anchor]
    return MetricDetailOut(
        key=key,
        area=area_of(key),
        unit=METRICS[key].unit,
        stream=STREAM_OF[key],
        state=state,
        progress_percent=stream.progress_percent,
        range=range_name,
        start=start,
        end=anchor,
        latest=latest_of(records),
        series=_series(inside, start, days),
        unconfirmed=[UnconfirmedDay(day=d, n=n) for d, n in sorted((unconfirmed or {}).items())],
        band=None if isinstance(band, InsufficientData) else band_out(band, anchor),
        band_progress=progress_of(band, BAND_DAYS) if isinstance(band, InsufficientData) else None,
        trend=trend_out,
        change_points=_change_points(key, inside, anchor, days),
        data_quality=summarise(inside, window_end=anchor, window_days=days).as_dict(),
        method=method_of(key, trend_out),
        provenance=ProvenanceOut(
            algo_versions=sorted({r.algo_version for r in shown}),
            input_revision=max((r.input_revision for r in shown), default=None),
            current_revision=revision,
            recompute_pending=recompute_pending(shown, revision),
        ),
    )
