"""Readiness (ADR 0012 §4): how last night compares with the user's own previous 28 nights.

Only when a sleep summary exists for the night whose wake-up falls on ``day``. Per component
(sleep duration, night heart rate, HRV where reported) the score is
``clamp(50 + 50 * sign * (x - m) / (3 * s), 0, 100)`` against the PRECEDING 28-night median ``m``
and scale ``s = max(1.4826 * MAD, floor)``. Readiness is the integer mean of the computed
components; duration and night heart rate must both be computed. No label such as "recovered".
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Final

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.dataquality import summarise
from ownvitals.analysis.methods.baseline import Window, rolling_baseline
from ownvitals.analysis.methods.series import Series
from ownvitals.analysis.policy import BASELINE_DEFAULT
from ownvitals.analysis.scores import ScoreResult, Status, clamp, result, round_half_up

ALGO_VERSION: Final = "readiness/1"
KIND: Final = "readiness"
WINDOW_DAYS: Final = 28
# (direction, scale floor): longer sleep and higher HRV, lower night heart rate (our choice).
COMPONENTS: Final = {"duration": (1.0, 0.25), "night_hr": (-1.0, 1.0), "hrv": (1.0, 2.0)}


@dataclass(frozen=True, slots=True)
class Night:
    """One sleep summary, on the local day of its wake-up. ``None``: the device did not report
    the field (never 0)."""

    day: date
    duration_h: float | None = None
    night_hr: float | None = None
    hrv_ms: float | None = None

    def get(self, name: str) -> float | None:
        return {"duration": self.duration_h, "night_hr": self.night_hr, "hrv": self.hrv_ms}[name]


def _daily(points: list[tuple[date, float]]) -> list[DailyValue]:
    return [
        DailyValue(
            d,
            Decimal(repr(x)),
            DailyMethod.MEDIAN,
            1,
            Decimal(repr(x)),
            Decimal(repr(x)),
            Decimal(0),
        )
        for d, x in points
    ]


def _component(name: str, nights: list[Night], last: Night, day: date) -> dict[str, Any]:
    sign, floor = COMPONENTS[name]
    x = last.get(name)
    points = [(n.day, v) for n in nights if (v := n.get(name)) is not None]
    quality = summarise(_daily(points), window_end=day - timedelta(days=1), window_days=WINDOW_DAYS)
    base: dict[str, Any] = {"value": x, "data_quality": quality.as_dict()}
    if x is None:
        return {**base, "status": Status.NOT_AVAILABLE.value, "reason": "not_reported"}
    if BASELINE_DEFAULT.shortfalls(quality):
        return {**base, "status": Status.INSUFFICIENT_DATA.value, "reason": "baseline_insufficient"}
    series = Series.from_daily(_daily(points), window_days=WINDOW_DAYS)
    at = float((day - date(1970, 1, 1)).days)
    b = rolling_baseline(
        series,
        window_days=WINDOW_DAYS,
        window=Window.PRECEDING,
        min_points=1,
        min_scale=floor,
        at=[at],
    )
    m, s = float(b.median[0]), float(b.scale[0])
    score = clamp(50 + 50 * sign * (x - m) / (3 * s))
    return {**base, "status": Status.COMPUTED.value, "usual": m, "scale": s, "score": score}


def evaluate_readiness(nights: Iterable[Night], *, day: date) -> ScoreResult:
    """Readiness for ``day`` from the sleep summaries ``nights`` (any order)."""
    ordered = sorted(nights, key=lambda n: n.day)
    last = next((n for n in ordered if n.day == day), None)
    if last is None:
        return result(KIND, ALGO_VERSION, Status.NOT_AVAILABLE, {"reason": "no_sleep_data"})
    window = [n for n in ordered if day - timedelta(days=WINDOW_DAYS) <= n.day < day]
    comps = {name: _component(name, window, last, day) for name in COMPONENTS}
    comps_out: dict[str, Any] = {"components": comps, "text_key": "score.readiness.how"}
    for name in ("duration", "night_hr"):
        c = comps[name]
        if c["status"] != Status.COMPUTED.value:
            reason = (
                "hr_missing"
                if name == "night_hr" and c["reason"] == "not_reported"
                else c["reason"]
            )
            return result(
                KIND,
                ALGO_VERSION,
                Status.INSUFFICIENT_DATA,
                {**comps_out, "reason": reason, "component": name},
            )
    used = [c["score"] for c in comps.values() if c["status"] == Status.COMPUTED.value]
    return result(
        KIND, ALGO_VERSION, Status.COMPUTED, comps_out, float(round_half_up(sum(used) / len(used)))
    )
