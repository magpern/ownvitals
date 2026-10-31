"""Minimum-data policy (PLAN §7.1 step 4). Pure and declarative.

A metric has one ``Requirement`` per purpose (``trend``, ``baseline``). Below it the result is
an ``InsufficientData`` carrying the numbers (``n``, ``coverage`` and the whole ``DataQuality``)
and the shortfalls: never a value and never a finding. A "point" is a day with data, i.e. a
daily value; the window is counted in calendar days and a span in days **elapsed** between the
first and the last day with data (see ``dataquality``): 28 days means a day with data and
another one 28 days later, not 27.

Defaults (PLAN §7.1 step 4): a **trend** needs at least 8 days with data whose span, first to
last day with data, is at least 60 % of the window; a **baseline** needs at least 10 days with
data spanning at least 21 days. ``POLICIES`` holds the per-metric overrides; N1 (PLAN §7.2)
asks the nerve metrics for at least 4 measures over at least 4 weeks (28 days), and their
baseline needs at least 28 days too, so a nerve baseline is never sufficient while N1 is not.

Every threshold is inclusive: exactly the minimum passes, one less fails.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from fractions import Fraction
from types import MappingProxyType

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.dataquality import DEFAULT_MIN_GAP_DAYS, DataQuality, summarise

ALGO_VERSION = "policy/2"


class Purpose(StrEnum):
    TREND = "trend"
    BASELINE = "baseline"


class Shortfall(StrEnum):
    DAYS_WITH_DATA = "days_with_data"
    SPAN_DAYS = "span_days"
    SPAN_FRACTION = "span_fraction"


@dataclass(frozen=True, slots=True)
class Requirement:
    min_days_with_data: int
    min_span_days: int | None = None
    min_span_fraction: Fraction | None = None  # of the window, in (0, 1)

    def __post_init__(self) -> None:
        if self.min_days_with_data < 1:
            raise ValueError("min_days_with_data must be at least 1")
        if self.min_span_days is not None and self.min_span_days < 1:
            raise ValueError("min_span_days must be at least 1")
        # A span is days elapsed, at most ``window_days - 1``, so ``span_fraction`` is at most
        # (w - 1) / w whatever the window: 1 or more can never be met. A fraction below 1 that a
        # short window cannot reach (3/5 of a 2-day window) stays valid: that window is
        # ``InsufficientData``, as with ``min_span_days`` larger than the window.
        if self.min_span_fraction is not None and not 0 < self.min_span_fraction < 1:
            raise ValueError(
                "min_span_fraction must be in (0, 1): a span is at most window_days - 1 days, "
                "so a fraction of 1 or more is never met"
            )

    def shortfalls(self, quality: DataQuality) -> tuple[Shortfall, ...]:
        found: list[Shortfall] = []
        if quality.n_days < self.min_days_with_data:
            found.append(Shortfall.DAYS_WITH_DATA)
        if self.min_span_days is not None and quality.span_days < self.min_span_days:
            found.append(Shortfall.SPAN_DAYS)
        if self.min_span_fraction is not None and quality.span_fraction < self.min_span_fraction:
            found.append(Shortfall.SPAN_FRACTION)
        return tuple(found)


TREND_DEFAULT = Requirement(min_days_with_data=8, min_span_fraction=Fraction(3, 5))
BASELINE_DEFAULT = Requirement(min_days_with_data=10, min_span_days=21)
NERVE_TREND = Requirement(min_days_with_data=4, min_span_days=28)  # N1, PLAN §7.2
# The nerve baseline must not be easier to meet than N1: the default's 21-day span would let a
# baseline pass while the trend still says "insufficient data".
NERVE_BASELINE = Requirement(
    min_days_with_data=BASELINE_DEFAULT.min_days_with_data, min_span_days=NERVE_TREND.min_span_days
)


@dataclass(frozen=True, slots=True)
class MetricPolicy:
    trend: Requirement = TREND_DEFAULT
    baseline: Requirement = BASELINE_DEFAULT

    def requirement(self, purpose: Purpose) -> Requirement:
        return self.trend if purpose is Purpose.TREND else self.baseline


DEFAULT_POLICY = MetricPolicy()

# Overrides by metric key (``quality.METRICS``); every other metric uses DEFAULT_POLICY.
POLICIES: Mapping[str, MetricPolicy] = MappingProxyType(
    {
        "nerve_health_score": MetricPolicy(trend=NERVE_TREND, baseline=NERVE_BASELINE),
        "nerve_response_score": MetricPolicy(trend=NERVE_TREND, baseline=NERVE_BASELINE),
        "esc": MetricPolicy(trend=NERVE_TREND, baseline=NERVE_BASELINE),
    }
)


def policy_for(metric_key: str) -> MetricPolicy:
    return POLICIES.get(metric_key, DEFAULT_POLICY)


@dataclass(frozen=True, slots=True)
class Sufficient:
    requirement: Requirement
    data_quality: DataQuality
    algo_version: str = ALGO_VERSION


@dataclass(frozen=True, slots=True)
class InsufficientData:
    """Not enough data. There is no value and no finding behind this."""

    requirement: Requirement
    shortfalls: tuple[Shortfall, ...]
    data_quality: DataQuality
    algo_version: str = ALGO_VERSION

    @property
    def n(self) -> int:
        """Days with data in the window."""
        return self.data_quality.n_days

    @property
    def coverage(self) -> Fraction:
        """Share of the window's days that have data."""
        return self.data_quality.coverage


type Sufficiency = Sufficient | InsufficientData


def evaluate(
    daily: Iterable[DailyValue],
    *,
    requirement: Requirement,
    window_end: date,
    window_days: int,
    min_gap_days: int = DEFAULT_MIN_GAP_DAYS,
) -> Sufficiency:
    """``Sufficient`` when the daily values in the window meet ``requirement``, otherwise
    ``InsufficientData``. Both carry the window's ``DataQuality``."""
    quality = summarise(
        daily, window_end=window_end, window_days=window_days, min_gap_days=min_gap_days
    )
    shortfalls = requirement.shortfalls(quality)
    if shortfalls:
        return InsufficientData(requirement, shortfalls, quality)
    return Sufficient(requirement, quality)


def evaluate_metric(
    metric_key: str,
    purpose: Purpose,
    daily: Iterable[DailyValue],
    *,
    window_end: date,
    window_days: int,
    min_gap_days: int = DEFAULT_MIN_GAP_DAYS,
) -> Sufficiency:
    """``evaluate`` with the requirement the metric's policy declares for ``purpose``."""
    return evaluate(
        daily,
        requirement=policy_for(metric_key).requirement(purpose),
        window_end=window_end,
        window_days=window_days,
        min_gap_days=min_gap_days,
    )
