"""Pure assembly of one metric's detail view (PLAN section 7.1 steps 3 to 5, section 8): the
chart window, the series, the trend, the change points and the method note.

Split from ``views.assemble`` (no behaviour change; its docstring describes the assembly). No
database, clock or Withings: records and the day "today" come in, response models go out.
"""

from collections.abc import Sequence
from datetime import date, timedelta

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.dataquality import summarise
from ownvitals.analysis.methods.changepoint import detect_changes
from ownvitals.analysis.methods.series import EPOCH, Series
from ownvitals.analysis.methods.trend import DEFAULT_ALPHA, MAX_POINTS, trend
from ownvitals.analysis.policy import InsufficientData, Purpose, evaluate_metric
from ownvitals.analysis.quality import METRICS
from ownvitals.analysis.rules.core import DISPLAY_K, display_band, min_half_width
from ownvitals.views import states
from ownvitals.views.assemble import (
    band_out,
    latest_of,
    metric_state,
    progress_of,
    recompute_pending,
    to_daily,
)
from ownvitals.views.catalog import BAND_DAYS, RANGE_DAYS, STREAM_OF, area_of
from ownvitals.views.reads import DailyRecord
from ownvitals.views.schemas import (
    ChangePointOut,
    ChangePointsOut,
    MethodOut,
    MetricDetailOut,
    ProvenanceOut,
    SeriesPoint,
    TrendOut,
    UnconfirmedDay,
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
