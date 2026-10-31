"""Robust baseline: rolling median and MAD over **time windows** (PLAN §7.1 step 5). Pure.

For each query time ``q`` the window holds the points whose time falls in an interval of
``window_days`` days around ``q``; the count of points in it is whatever the sampling gives.
That is what makes the result independent of the sampling density: a point count window
("the last 30 readings") covers a day or a year depending on how often the user weighs in.

* the **baseline** is the median of the window's values and ``mad`` the raw median absolute
  deviation about it;
* the **typical range** is ``median ± k · scale`` with ``scale = MAD_SCALE · mad`` (the
  standard-deviation equivalent of the MAD for normal data, Rousseeuw and Croux 1993) and
  ``k = 2`` by default, roughly the central 95 % of a normal distribution. ``min_scale`` puts a
  floor under the scale (e.g. the scale's resolution) so a window of identical values does not
  give a zero-width range;
* a window with fewer than ``min_points`` points has no baseline: its statistics are NaN and
  ``valid`` is False. Nothing is interpolated across a gap.

Window alignments (all intervals are closed unless said otherwise):

* ``TRAILING``: ``[q - W, q]``, the point at ``q`` included;
* ``PRECEDING``: ``[q - W, q)``, the history before ``q`` without the point at ``q``, the one to
  compare a new value with ("higher than your 90-day typical range");
* ``CENTERED``: ``[q - W/2, q + W/2]``.

Times are floats (days), so the boundaries are exact for whole days and for the usual
timestamps, and subject to float rounding otherwise.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np
from numpy.typing import ArrayLike

from ownvitals.analysis.methods.series import (
    MAD_SCALE,
    FloatArray,
    InputSummary,
    Series,
    list_or_none,
    mad,
)

ALGO_VERSION = "baseline/1"

DEFAULT_K = 2.0
DEFAULT_MIN_POINTS = 3


class Window(StrEnum):
    TRAILING = "trailing"
    PRECEDING = "preceding"
    CENTERED = "centered"


@dataclass(frozen=True, slots=True, eq=False)
class BaselineResult:
    """One entry per query time. Where ``valid`` is False the statistics are NaN."""

    at: FloatArray
    median: FloatArray
    mad: FloatArray  # raw, unscaled
    scale: FloatArray  # max(MAD_SCALE * mad, min_scale)
    low: FloatArray  # median - k * scale
    high: FloatArray  # median + k * scale
    n_window: tuple[int, ...]  # points in each window
    valid: tuple[bool, ...]
    window_days: float
    window: Window
    k: float
    min_points: int
    min_scale: float
    inputs: InputSummary
    algo_version: str = ALGO_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": list_or_none(self.at),
            "median": list_or_none(self.median),
            "mad": list_or_none(self.mad),
            "scale": list_or_none(self.scale),
            "low": list_or_none(self.low),
            "high": list_or_none(self.high),
            "n_window": list(self.n_window),
            "valid": list(self.valid),
            "window_days": self.window_days,
            "window": self.window.value,
            "k": self.k,
            "min_points": self.min_points,
            "min_scale": self.min_scale,
            "inputs": self.inputs.as_dict(),
            "algo_version": self.algo_version,
        }


def _bounds(
    t: FloatArray, at: FloatArray, window_days: float, window: Window
) -> tuple[np.ndarray, np.ndarray]:
    """Index ranges ``[lo, hi)`` of the points inside each window; ``t`` is sorted."""
    if window is Window.CENTERED:
        start, end = at - window_days / 2, at + window_days / 2
        return np.searchsorted(t, start, "left"), np.searchsorted(t, end, "right")
    start = at - window_days
    end_side = "right" if window is Window.TRAILING else "left"
    return np.searchsorted(t, start, "left"), np.searchsorted(t, at, end_side)


def rolling_baseline(
    series: Series,
    *,
    window_days: float,
    window: Window = Window.TRAILING,
    min_points: int = DEFAULT_MIN_POINTS,
    k: float = DEFAULT_K,
    min_scale: float = 0.0,
    at: ArrayLike | Sequence[float] | None = None,
) -> BaselineResult:
    """The baseline at each time of ``at`` (default: each time of ``series``).

    ``at`` may name any times, in the series' own unit (days); it need not be sorted.
    """
    if not window_days > 0:
        raise ValueError("window_days must be positive")
    if min_points < 1:
        raise ValueError("min_points must be at least 1")
    if not k > 0:
        raise ValueError("k must be positive")
    if min_scale < 0:
        raise ValueError("min_scale must not be negative")
    queries = series.t if at is None else np.asarray(at, dtype=np.float64)
    if queries.ndim != 1 or not np.isfinite(queries).all():
        raise ValueError("at must be a one-dimensional array of finite times")

    lo, hi = _bounds(series.t, queries, float(window_days), window)
    size = queries.size
    median = np.full(size, np.nan)
    spread = np.full(size, np.nan)
    counts: list[int] = []
    valid: list[bool] = []
    for i in range(size):
        values = series.y[lo[i] : hi[i]]
        counts.append(int(values.size))
        ok = values.size >= min_points
        valid.append(ok)
        if ok:
            median[i] = np.median(values)
            spread[i] = mad(values)
    scale = np.where(np.isnan(spread), np.nan, np.maximum(MAD_SCALE * spread, min_scale))
    return BaselineResult(
        at=np.array(queries, dtype=np.float64),
        median=median,
        mad=spread,
        scale=scale,
        low=median - k * scale,
        high=median + k * scale,
        n_window=tuple(counts),
        valid=tuple(valid),
        window_days=float(window_days),
        window=window,
        k=float(k),
        min_points=min_points,
        min_scale=float(min_scale),
        inputs=series.summary(),
    )
