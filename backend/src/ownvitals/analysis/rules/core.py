"""Shared logic of the v1 finding rules (ADR 0010 §2 to §6; PLAN §7.2). Pure.

* the **two bands** (§2): ``display_band`` (k 1.5) and ``finding_band`` (k 2.5, the 90 days
  before the reference day), both ``median ± max(w, k · 1.4826 · MAD)`` with the metric's minimum
  half-width *w*, gated by the metric's BASELINE policy; ``k`` is always passed explicitly;
* **confidence** (§5) and **severity** (§6);
* the result types: a rule returns a ``Finding`` proposal, a ``NoFinding`` or ``Insufficient``
  (insufficient data, never a finding);
* ``trend_rule``: the one implementation of "a ``trend`` test plus an effect threshold" that
  B2, B4, B5 and C2 share.

Daily series are built with ``Series.from_daily``, never ``from_instants`` (which counts UTC
days). Nothing here knows who may see a rule: an experimental rule only carries its tier.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

import numpy as np

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.dataquality import DataQuality
from ownvitals.analysis.methods.baseline import Window, rolling_baseline
from ownvitals.analysis.methods.series import EPOCH, MAD_SCALE, Series
from ownvitals.analysis.methods.trend import Correction, Direction, TrendResult, trend
from ownvitals.analysis.policy import (
    InsufficientData,
    Purpose,
    Sufficient,
    evaluate_metric,
)

ALGO_VERSION = "rules/1"

DISPLAY_K = 1.5  # charts, tiles, range bars (ADR 0010 §2)
FINDING_K = 2.5  # "above/below the personal 90-day range" (Leys et al. 2013)
BAND_DAYS = 90
TREND_ALPHA = 0.05
MORE_THAN_LOW_TREND = 20  # fewer points with phi above LOW_TREND_PHI is low confidence (#87)
LOW_TREND_PHI = 0.5

# Minimum half-width w of both bands (ADR 0010 §2): the mock-ups' ``bandMin`` where they have
# one, otherwise one display step (our choice).
MIN_HALF_WIDTH: Mapping[str, float] = MappingProxyType(
    {
        "weight": 0.45,
        "fat_ratio": 0.35,
        "muscle_mass": 0.3,
        "visceral_fat": 0.5,
        "heart_pulse": 2.0,
        "fat_free_mass": 0.1,
        "hydration": 0.1,
        "extracellular_water": 0.1,
        "intracellular_water": 0.1,
        "metabolic_age": 1.0,
        "vascular_age": 1.0,
        "qtc_interval": 1.0,
        "qrs_duration": 1.0,
        "nerve_health_score": 1.0,
        "nerve_response_score": 1.0,
        "esc": 1.0,
    }
)


class Tier(StrEnum):
    DEVICE = "device"
    TREND = "trend"
    EXPERIMENTAL = "experimental"
    SYSTEM = "system"


class Severity(StrEnum):
    URGENT = "urgent"
    ATTENTION = "attention"
    INFO = "info"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


@dataclass(frozen=True, slots=True)
class RuleSpec:
    rule_id: str
    version: int
    tier: Tier
    severity: Severity
    persistent: bool = False  # the condition includes a ``trend`` test (§4 persistence)


def _spec(
    rule_id: str, tier: Tier, *, persistent: bool = False, severity: Severity = Severity.ATTENTION
) -> tuple[str, RuleSpec]:
    return rule_id, RuleSpec(rule_id, 1, tier, severity, persistent)


# Rules of this module's three files. A change to a rule bumps its ``version``.
SPECS: Mapping[str, RuleSpec] = MappingProxyType(
    dict(
        [
            _spec("B1", Tier.EXPERIMENTAL),
            _spec("B2", Tier.TREND, persistent=True),
            _spec("B3", Tier.EXPERIMENTAL),
            _spec("B4", Tier.TREND, persistent=True),
            _spec("B5", Tier.TREND, persistent=True),
            _spec("B6", Tier.TREND),
            _spec("C1", Tier.TREND),
            _spec("C1x", Tier.EXPERIMENTAL),
            _spec("C2", Tier.TREND, persistent=True),
        ]
    )
)

# Severity of every v1 rule (ADR 0010 §6): never a diagnosis, never a clinical band. Q1 has one
# entry per part (``Q1:<part>``).
SEVERITY: Mapping[str, Severity] = MappingProxyType(
    {
        "E1": Severity.URGENT,
        "Q1:reconnect": Severity.URGENT,
        "Q1:no_weigh_in": Severity.INFO,
        "Q1:low_battery": Severity.INFO,
        "Q1:import_incomplete": Severity.INFO,
        **{
            rule: Severity.ATTENTION
            for rule in (
                *("E2", "E3", "E4", "E6", "B1", "B2", "B3", "B4", "B5", "B6"),
                *("N2", "N3", "N5", "C1", "C1x", "C2"),
            )
        },
    }
)


def min_half_width(metric_key: str) -> float:
    try:
        return MIN_HALF_WIDTH[metric_key]
    except KeyError:
        raise KeyError(f"no minimum half-width for {metric_key}: add it with its source") from None


# --- days and daily values ---------------------------------------------------------------------


def day_number(day: date) -> int:
    """Days since 1970-01-01: the time axis of ``Series.from_daily``."""
    return (day - EPOCH).days


def window_of(daily: Iterable[DailyValue], end: date, days: int) -> list[DailyValue]:
    """The daily values on the ``days`` calendar days ending on ``end`` (both ends inclusive)."""
    start = end - timedelta(days=days - 1)
    return [v for v in daily if start <= v.day <= end]


def value_on(daily: Iterable[DailyValue], day: date) -> DailyValue | None:
    return next((v for v in daily if v.day == day), None)


def require_value(daily: Iterable[DailyValue], day: date, what: str) -> DailyValue:
    """The trigger's daily value: a rule is evaluated only on a day that has one (§1)."""
    found = value_on(daily, day)
    if found is None:
        raise ValueError(f"{what} has no daily value on {day}: evaluate only days with data")
    return found


# --- provenance ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """What a daily value came from: the measure groups it used (ADR 0006 §11's trigger rule) and
    whether one of its readings carried #37's advisory ``jump`` flag."""

    groups: tuple[int, ...] = ()
    jump: bool = False


type ProvenanceMap = Mapping[str, Mapping[date, Provenance]]


def provenance_of(provenance: ProvenanceMap | None, key: str, day: date) -> Provenance:
    return (provenance or {}).get(key, {}).get(day, Provenance())


def groups_on(provenance: ProvenanceMap | None, key: str, days: Iterable[date]) -> list[int]:
    found = {g for day in days for g in provenance_of(provenance, key, day).groups}
    return sorted(found)


# --- results ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """A finding proposal: the rule fired on ``day``. ``window_*`` are the days its condition
    used, ending at ``day``; ``reference_day`` is the day its band was taken at (§2). For a
    trend-test rule (``SPECS[rule_id].persistent``) it is the evaluation's condition, and an
    episode opens only through ``advance_persistent``."""

    rule_id: str
    rule_version: int
    tier: Tier
    subject: str
    day: date
    window_start: date
    window_end: date
    reference_day: date
    confidence: Confidence
    severity: Severity
    evidence: dict[str, Any]
    data_quality: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NoFinding:
    rule_id: str
    subject: str
    day: date
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Insufficient:
    """Insufficient data (or a rule not available to the user): never a finding."""

    rule_id: str
    subject: str
    day: date
    reason: str
    shortfalls: tuple[str, ...] = ()
    data_quality: dict[str, Any] | None = None


type RuleResult = Finding | NoFinding | Insufficient


def insufficient_from(
    rule_id: str, subject: str, day: date, verdict: InsufficientData, what: str
) -> Insufficient:
    return Insufficient(
        rule_id,
        subject,
        day,
        reason=what,
        shortfalls=tuple(s.value for s in verdict.shortfalls),
        data_quality=verdict.data_quality.as_dict(),
    )


def make_finding(
    rule_id: str,
    subject: str,
    day: date,
    *,
    window_start: date,
    window_end: date,
    reference_day: date,
    confidence: Confidence,
    evidence: dict[str, Any],
    data_quality: DataQuality | None,
    trigger_groups: Iterable[int] = (),
) -> Finding:
    spec = SPECS[rule_id]
    return Finding(
        rule_id=rule_id,
        rule_version=spec.version,
        tier=spec.tier,
        subject=subject,
        day=day,
        window_start=window_start,
        window_end=window_end,
        reference_day=reference_day,
        confidence=confidence,
        severity=spec.severity,
        evidence={**evidence, "trigger_groups": sorted(set(trigger_groups))},
        data_quality={} if data_quality is None else data_quality.as_dict(),
    )


# --- the two bands (§2) ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Band:
    """``median ± half_width`` of a metric's daily values, gated by its BASELINE policy."""

    metric_key: str
    reference_day: date
    window: Window
    k: float
    min_half_width: float
    median: float
    mad: float  # raw, unscaled
    half_width: float
    verdict: Sufficient

    @property
    def low(self) -> float:
        return self.median - self.half_width

    @property
    def high(self) -> float:
        return self.median + self.half_width

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric_key,
            "reference_day": self.reference_day.isoformat(),
            "window": self.window.value,
            "k": self.k,
            "min_half_width": self.min_half_width,
            "median": self.median,
            "low": self.low,
            "high": self.high,
        }


def band_at(
    metric_key: str, daily: Iterable[DailyValue], q: date, *, k: float, window: Window
) -> Band | InsufficientData:
    """The band at ``q`` over 90 calendar days: ``TRAILING`` is ``q - 89 … q``, ``PRECEDING`` is
    ``q - 90 … q - 1``. Below the metric's BASELINE policy on those days: ``InsufficientData``."""
    if window is Window.CENTERED:
        raise ValueError("a band is trailing or preceding, never centered")
    values = list(daily)
    end = q if window is Window.TRAILING else q - timedelta(days=1)
    verdict = evaluate_metric(metric_key, Purpose.BASELINE, values, window_end=end, window_days=90)
    if isinstance(verdict, InsufficientData):
        return verdict
    width = min_half_width(metric_key)
    series = Series.from_daily(window_of(values, end, BAND_DAYS), window_days=BAND_DAYS)
    result = rolling_baseline(
        series,
        window_days=BAND_DAYS - 1 if window is Window.TRAILING else BAND_DAYS,
        window=window,
        min_points=1,
        k=k,
        min_scale=width / k,
        at=[day_number(q)],
    )
    spread = float(result.mad[0])
    return Band(
        metric_key=metric_key,
        reference_day=q,
        window=window,
        k=k,
        min_half_width=width,
        median=float(result.median[0]),
        mad=spread,
        half_width=max(width, k * MAD_SCALE * spread),
        verdict=verdict,
    )


def display_band(
    metric_key: str, daily: Iterable[DailyValue], q: date, *, window: Window = Window.TRAILING
) -> Band | InsufficientData:
    """The displayed typical range (k 1.5): the chart's (``TRAILING``) or a comparison
    sentence's (``PRECEDING``)."""
    return band_at(metric_key, daily, q, k=DISPLAY_K, window=window)


def finding_band(
    metric_key: str, daily: Iterable[DailyValue], reference_day: date
) -> Band | InsufficientData:
    """The finding threshold (k 2.5): the 90 days before the episode's reference day."""
    return band_at(metric_key, daily, reference_day, k=FINDING_K, window=Window.PRECEDING)


# --- confidence (§5) ----------------------------------------------------------------------------


def lowest(*levels: Confidence) -> Confidence:
    return min(levels, key=lambda level: _RANK[level])


def confidence_data(*verdicts: Sufficient) -> Confidence:
    """D: ``high`` when every window holds at least twice its requirement's days with data."""
    enough = all(v.data_quality.n_days >= 2 * v.requirement.min_days_with_data for v in verdicts)
    return Confidence.HIGH if enough else Confidence.MEDIUM


def confidence_trend(result: TrendResult) -> Confidence:
    """T: ``low`` on under 20 points with phi above 0.5 (#87), ``high`` when p < alpha/5."""
    phi = result.ar1_phi
    if result.inputs.n < MORE_THAN_LOW_TREND and phi is not None and phi > LOW_TREND_PHI:
        return Confidence.LOW
    return Confidence.HIGH if result.p_value < result.alpha / 5 else Confidence.MEDIUM


def confidence_run(length: int, minimum: int) -> Confidence:
    """R: ``high`` from twice the rule's minimum run (C1: 6 days; N2: 4)."""
    return Confidence.HIGH if length >= 2 * minimum else Confidence.MEDIUM


def confidence_jump(jumped: bool) -> Confidence:
    """J: a trigger reading with #37's ``jump`` flag makes the finding low."""
    return Confidence.LOW if jumped else Confidence.HIGH


# --- trend-test rules --------------------------------------------------------------------------

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
