"""The shared trend-test rule of the v1 finding rules (ADR 0010 §3 and §7). Pure.

* ``trend_of``: ``trend`` (AR(1) correction) over the daily values of a window, built with
  ``Series.from_daily`` (never ``from_instants``);
* ``relative_change``: ``slope * days / intercept``;
* ``trend_rule``: the one implementation of "a ``trend`` test plus an effect threshold" that B2,
  B4, B5 and C2 share. Split out of ``core`` to keep that file under 500 lines.
"""

from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.methods.series import Series
from ownvitals.analysis.methods.trend import Correction, Direction, TrendResult, trend
from ownvitals.analysis.policy import InsufficientData, Purpose, evaluate_metric
from ownvitals.analysis.rules.core import (
    TREND_ALPHA,
    NoFinding,
    ProvenanceMap,
    RuleResult,
    confidence_data,
    confidence_jump,
    confidence_trend,
    insufficient_from,
    lowest,
    make_finding,
    provenance_of,
    require_value,
    window_of,
)

type Transform = Callable[[float, float], float]


def trend_of(
    daily: Sequence[DailyValue],
    end: date,
    days: int,
    *,
    alpha: float = TREND_ALPHA,
    transform: Transform | None = None,
) -> TrendResult:
    """``trend`` (AR(1) correction) of the daily values on the ``days`` days ending at ``end``.
    ``transform(t, y)`` maps the day number and value first (C2 subtracts the age)."""
    inside = window_of(daily, end, days)
    series = Series.from_daily(inside, window_days=days)
    if transform is not None:
        y = np.array(
            [transform(float(t), float(v)) for t, v in zip(series.t, series.y, strict=True)]
        )
        series = Series.from_arrays(series.t, y, window_days=days)
    return trend(series, alpha=alpha, correction=Correction.AR1)


def relative_change(result: TrendResult, days: int) -> float:
    """``slope · days / intercept``: the Theil-Sen change over ``days`` relative to the fitted
    level at the first point (B2, B4)."""
    if result.intercept == 0.0:
        return float("nan")
    return result.slope * days / result.intercept


def trend_rule(
    rule_id: str,
    key: str,
    daily: Sequence[DailyValue],
    day: date,
    provenance: ProvenanceMap | None,
    *,
    window_days: int,
    direction: Direction,
    ratio_at_most: float | None = None,
    transform: Transform | None = None,
    jump_applies: bool = False,
) -> RuleResult:
    """A ``trend`` test in ``direction`` at alpha 0.05 over the ``window_days`` days ending at
    ``day`` (TREND policy), plus the rule's effect threshold: ``relative_change <=
    ratio_at_most``. Confidence is the lowest of D, T and (for weight) J."""
    require_value(daily, day, key)
    verdict = evaluate_metric(key, Purpose.TREND, daily, window_end=day, window_days=window_days)
    if isinstance(verdict, InsufficientData):
        return insufficient_from(rule_id, key, day, verdict, f"{key} trend policy")
    result = trend_of(daily, day, window_days, transform=transform)
    ratio = relative_change(result, window_days)
    holds = result.direction is direction and (ratio_at_most is None or ratio <= ratio_at_most)
    if not holds:
        return NoFinding(rule_id, key, day, "trend condition not met")
    here = provenance_of(provenance, key, day)
    levels = [confidence_data(verdict), confidence_trend(result)]
    if jump_applies:
        levels.append(confidence_jump(here.jump))
    evidence: dict[str, Any] = {
        "trend": {
            "direction": result.direction.value,
            "p_value": result.p_value,
            "alpha": result.alpha,
            "slope_per_day": result.slope,
            "intercept": result.intercept,
            "n": result.inputs.n,
            "ar1_phi": result.ar1_phi,
        },
        "window_days": window_days,
        "change_ratio": ratio,
        "change_ratio_limit": ratio_at_most,
    }
    return make_finding(
        rule_id,
        key,
        day,
        window_start=day - timedelta(days=window_days - 1),
        window_end=day,
        reference_day=day,
        confidence=lowest(*levels),
        evidence=evidence,
        data_quality=verdict.data_quality,
        trigger_groups=here.groups,
    )
