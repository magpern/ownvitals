"""The two bands of the v1 finding rules (ADR 0010 §2). Pure.

``display_band`` (k 1.5) and ``finding_band`` (k 2.5, the 90 days before the reference day), both
``median ± max(w, k · 1.4826 · MAD)`` with the metric's minimum half-width *w*, gated by the
metric's BASELINE policy; ``k`` is always passed explicitly. Split out of
``ownvitals.analysis.rules.core``, which re-exports every name here.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType
from typing import Any

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.methods.baseline import Window, rolling_baseline
from ownvitals.analysis.methods.series import MAD_SCALE, Series
from ownvitals.analysis.policy import InsufficientData, Purpose, Sufficient, evaluate_metric
from ownvitals.analysis.rules.dailyvalues import day_number, window_of

DISPLAY_K = 1.5  # charts, tiles, range bars (ADR 0010 §2)
FINDING_K = 2.5  # "above/below the personal 90-day range" (Leys et al. 2013)
BAND_DAYS = 90

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


def min_half_width(metric_key: str) -> float:
    try:
        return MIN_HALF_WIDTH[metric_key]
    except KeyError:
        raise KeyError(f"no minimum half-width for {metric_key}: add it with its source") from None


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
