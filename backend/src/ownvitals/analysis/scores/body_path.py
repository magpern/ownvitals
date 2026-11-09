"""Body Path (ADR 0012 §3): a projection of the user's own recent trajectory with an 80 %
prediction interval. Pure; the merged ``trend`` is the only method used.

Interval at horizon h: ``[L + b_low*h - z*sigma, L + b_high*h + z*sigma]``, the slope interval
and the day-to-day noise added, never combined in quadrature (that under-covers, ADR 0012 §3).
No goal date, no target, no change-point cut (``detect_changes`` is recorded only).
"""

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any, Final

import numpy as np

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.dataquality import summarise
from ownvitals.analysis.methods.changepoint import detect_changes
from ownvitals.analysis.methods.series import MAD_SCALE, Series
from ownvitals.analysis.methods.trend import Correction, trend
from ownvitals.analysis.policy import InsufficientData, Purpose, evaluate_metric
from ownvitals.analysis.scores import ScoreResult, Status, result

ALGO_VERSION: Final = "bodypath/1"
WINDOW_DAYS: Final = 90
MIN_DAYS_WITH_DATA: Final = 14
HORIZONS: Final = (30, 60, 90)
Z80: Final = 1.2816
ALPHA: Final = 0.2
# ADR 0010 §2's minimum half-widths (kg); the residual scale never goes below width / 1.5.
MIN_WIDTH: Final = {"weight": 0.45, "muscle_mass": 0.3, "fat_mass": 0.1}
METRICS: Final = tuple(MIN_WIDTH)
_EPOCH: Final = date(1970, 1, 1)


def kind_of(metric_key: str) -> str:
    return f"body_path:{metric_key}"


def evaluate_body_path(metric_key: str, daily: Iterable[DailyValue], *, day: date) -> ScoreResult:
    """The projection of ``metric_key`` from the 90 days ending ``day``."""
    kind = kind_of(metric_key)
    start = day - timedelta(days=WINDOW_DAYS - 1)
    window = tuple(sorted((d for d in daily if start <= d.day <= day), key=lambda d: d.day))
    quality = summarise(window, window_end=day, window_days=WINDOW_DAYS)
    base: dict[str, Any] = {"window": {"start": start.isoformat(), "end": day.isoformat()}}
    if not window:
        return result(kind, ALGO_VERSION, Status.NOT_AVAILABLE, {**base, "reason": "no_data"})
    verdict = evaluate_metric(
        metric_key, Purpose.TREND, window, window_end=day, window_days=WINDOW_DAYS
    )
    shortfalls = [s.value for s in getattr(verdict, "shortfalls", ())]
    if quality.n_days < MIN_DAYS_WITH_DATA:
        shortfalls.append("min_days_14")
    if shortfalls:
        return result(
            kind,
            ALGO_VERSION,
            Status.INSUFFICIENT_DATA,
            {**base, "shortfalls": sorted(set(shortfalls)), "data_quality": quality.as_dict()},
        )
    series = Series.from_daily(window, window_days=WINDOW_DAYS)
    fit = trend(series, alpha=ALPHA, correction=Correction.AR1)
    end = float((day - _EPOCH).days)
    level = fit.intercept + fit.slope * (end - fit.t_ref)
    resid = series.y - (fit.intercept + fit.slope * (series.t - fit.t_ref))
    mad = float(np.median(np.abs(resid - np.median(resid))))
    sigma = max(MAD_SCALE * mad, MIN_WIDTH[metric_key] / 1.5)
    horizons = {
        str(h): {
            "centre": level + fit.slope * h,
            "low": level + fit.slope_low * h - Z80 * sigma,
            "high": level + fit.slope_high * h + Z80 * sigma,
        }
        for h in HORIZONS
    }
    changes = detect_changes(window, metric_key=metric_key, window_end=day, window_days=WINDOW_DAYS)
    components = {
        **base,
        "slope_per_day": fit.slope,
        "slope_interval": [fit.slope_low, fit.slope_high],
        "level": level,
        "sigma": sigma,
        "horizons": horizons,
        "ar1_phi": fit.ar1_phi,
        "n_over_n_star": fit.n_over_n_star,
        "changes": (
            {"insufficient_data": True}
            if isinstance(changes, InsufficientData)
            else changes.as_dict()
        ),
        "data_quality": quality.as_dict(),
        "modules": {"trend": fit.algo_version},
        "text_key": "score.body_path.how",
    }
    return result(kind, ALGO_VERSION, Status.COMPUTED, components, horizons["30"]["centre"])
