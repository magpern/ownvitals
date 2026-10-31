"""PELT change points (PLAN §7.1 step 5). Pure.

Finds the points where the **mean** of a series shifts, with PELT (Pruned Exact Linear Time;
Killick, Fearnhead and Eckley 2012, "Optimal detection of changepoints with a linear
computational cost", JASA 107:1590). It minimises, over all segmentations of the ``n`` points in
time order into segments ``y[a:b]``,

    Σ_segments cost(y[a:b])  +  β · (number of change points)

and prunes the candidates that can never be the last change point again (their Theorem 3.1 with
``K = 0``), so the result is **exact** (the same optimum as the O(n²) optimal-partitioning
recursion of Jackson et al. 2005, which the tests check on random series) at a cost that is
linear in ``n`` when change points are frequent.

The model and its parameters, all of which are choices of ours (the paper leaves them open):

* **Cost**: Gaussian mean shift, ``cost(y[a:b]) = Σ (y - mean)² / sigma²``, which is -2 times
  the Gaussian log-likelihood of a segment with its own mean and a common variance ``sigma²``.
  Points are taken in time order and the cost ignores the spacing, so irregular sampling and
  gaps need no special handling; a change point lies *between* two points, reported as the first
  point of the new segment (``index``, ``time``) with the last point before it
  (``previous_time``). How exactly the change happened within that gap is not known.
* **sigma** is estimated robustly from the first differences, ``MAD(Δy) · 1.4826 / √2``, which a few
  level shifts hardly move (a mean shift adds one large difference each). If more than half of
  the differences are zero (heavily tied values) the MAD is zero and the standard deviation of the
  differences over √2 is used instead. A constant series has no change points. Pass ``sigma`` to
  override.
* **Penalty**: ``β = 3 ln n`` by default, in units of ``sigma²``: a BIC-type penalty (Schwarz 1978)
  with the factor 3 making it conservative. ``penalty`` overrides it.
* **Minimum segment length** ``min_segment`` (default 5 points): a level is not claimed from
  fewer. With a minimum segment length the pruning is delayed by ``min_segment - 1`` steps, which
  keeps it exact (a pruned candidate is dominated only by a later candidate that is itself allowed
  to start a segment).

**Assumption**: independent noise around each level. Autocorrelated noise (day-to-day body weight
is) makes this cost see level shifts that are slow drifts; ``penalty`` is the lever, and nothing
here corrects for autocorrelation (use the modified Mann-Kendall in ``trend`` for "is there a
trend"). The wiring that turns change points into findings must say so.

**Minimum data** (PLAN §7.1 step 5): ``detect_changes`` runs the search only when the daily values
pass the minimum-data policy of ``ownvitals.analysis.policy`` and otherwise returns its
``InsufficientData``, unchanged. ``find_changes`` is the search itself, without the gate.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

import numpy as np

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.dataquality import DataQuality
from ownvitals.analysis.methods.series import (
    MAD_SCALE,
    FloatArray,
    InputSummary,
    Series,
    finite_or_none,
    mad,
)
from ownvitals.analysis.policy import (
    InsufficientData,
    Purpose,
    Requirement,
    evaluate,
    evaluate_metric,
)

ALGO_VERSION = "changepoint/1"

DEFAULT_MIN_SEGMENT = 5
PENALTY_FACTOR = 3.0  # β = PENALTY_FACTOR · ln n


@dataclass(frozen=True, slots=True)
class ChangePoint:
    """A shift of the mean between two points of the series."""

    index: int  # position of the first point of the new segment
    time: float  # its time (days)
    previous_time: float  # time of the last point before it
    mean_before: float  # mean of the segment before (up to the previous change point)
    mean_after: float  # mean of the segment after
    shift: float  # mean_after - mean_before
    shift_sigmas: float | None  # shift / sigma, None when sigma is 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "time": self.time,
            "previous_time": self.previous_time,
            "mean_before": self.mean_before,
            "mean_after": self.mean_after,
            "shift": self.shift,
            "shift_sigmas": self.shift_sigmas,
        }


@dataclass(frozen=True, slots=True)
class ChangePointResult:
    """The change points found, in time order; none is a result too (``searched`` says whether
    the series was long enough, at least ``2 · min_segment`` points, to find one)."""

    change_points: tuple[ChangePoint, ...]
    searched: bool
    sigma: float
    penalty: float
    min_segment: int
    cost: float  # Σ cost of the chosen segments, in units of sigma², without the penalty
    inputs: InputSummary
    data_quality: DataQuality | None = None  # set by detect_changes: the policy's verdict
    algo_version: str = ALGO_VERSION

    @property
    def indices(self) -> tuple[int, ...]:
        return tuple(cp.index for cp in self.change_points)

    def as_dict(self) -> dict[str, Any]:
        return {
            "change_points": [cp.as_dict() for cp in self.change_points],
            "searched": self.searched,
            "sigma": finite_or_none(self.sigma),
            "penalty": self.penalty,
            "min_segment": self.min_segment,
            "cost": self.cost,
            "inputs": self.inputs.as_dict(),
            "data_quality": None if self.data_quality is None else self.data_quality.as_dict(),
            "algo_version": self.algo_version,
        }


def estimate_sigma(values: FloatArray) -> float:
    """The robust, difference-based noise level described in the module docstring; 0 for a
    constant series."""
    if values.size < 2:
        return 0.0
    differences = np.diff(values)
    sigma = MAD_SCALE * mad(differences) / math.sqrt(2.0)
    if sigma == 0.0:
        sigma = float(np.std(differences)) / math.sqrt(2.0)
    return sigma


def default_penalty(n: int) -> float:
    return PENALTY_FACTOR * math.log(max(n, 2))


def pelt(
    standardised: FloatArray, *, penalty: float, min_segment: int
) -> tuple[tuple[int, ...], float]:
    """PELT with the Gaussian mean-shift cost ``Σ (z - mean)²`` on already standardised values.

    Returns the change points (the first index of each new segment, ascending) and the total
    cost of the chosen segments without the penalty. Fewer than ``2 · min_segment`` values have
    no change point.
    """
    n = int(standardised.size)
    if min_segment < 1:
        raise ValueError("min_segment must be at least 1")
    if not penalty > 0:
        raise ValueError("penalty must be positive")
    if n == 0:
        return (), 0.0
    centred = standardised - standardised.mean()
    c1 = np.concatenate(([0.0], np.cumsum(centred)))
    c2 = np.concatenate(([0.0], np.cumsum(centred * centred)))

    def segment_cost(starts: np.ndarray, end: int) -> np.ndarray:
        length = end - starts
        total = c1[end] - c1[starts]
        return np.maximum((c2[end] - c2[starts]) - total * total / length, 0.0)

    best = np.full(n + 1, np.inf)  # F(t)
    best[0] = -penalty
    previous = np.zeros(n + 1, dtype=np.int64)  # the last change point of the optimum up to t
    candidates = np.array([0], dtype=np.int64)
    dead_from = np.array([n + 1 + min_segment], dtype=np.int64)  # a candidate is unusable from here

    for t in range(min_segment, n + 1):
        alive = t < dead_from
        candidates, dead_from = candidates[alive], dead_from[alive]
        usable = t - candidates >= min_segment
        starts = candidates[usable]
        totals = best[starts] + segment_cost(starts, t)
        position = int(np.argmin(totals))
        best[t] = totals[position] + penalty
        previous[t] = starts[position]
        # Killick et al. Theorem 3.1 with K = 0: a candidate whose total exceeds F(t) can never
        # again be the best last change point, but only once a segment may start at t.
        pruned = np.flatnonzero(usable)[totals > best[t]]
        dead_from[pruned] = np.minimum(dead_from[pruned], t + min_segment)
        candidates = np.append(candidates, t)
        dead_from = np.append(dead_from, n + 1 + min_segment)

    points: list[int] = []
    end = n
    while n >= min_segment and previous[end] > 0:
        end = int(previous[end])
        points.append(end)
    points.reverse()
    edges = [0, *points, n]
    cost = sum(float(segment_cost(np.array([a]), b)[0]) for a, b in pairwise(edges))
    return tuple(points), cost


def find_changes(
    series: Series,
    *,
    penalty: float | None = None,
    min_segment: int = DEFAULT_MIN_SEGMENT,
    sigma: float | None = None,
) -> ChangePointResult:
    """The mean shifts of ``series``, without the minimum-data gate (see ``detect_changes``)."""
    if sigma is not None and not sigma > 0:
        raise ValueError("sigma must be positive")
    if min_segment < 1:
        raise ValueError("min_segment must be at least 1")
    n = series.n
    beta = default_penalty(n) if penalty is None else float(penalty)
    if not beta > 0:
        raise ValueError("penalty must be positive")
    noise = estimate_sigma(series.y) if sigma is None else float(sigma)
    points: tuple[int, ...] = ()
    cost = 0.0
    if n >= 2 * min_segment and noise > 0.0:
        points, cost = pelt(
            (series.y - series.y.mean()) / noise, penalty=beta, min_segment=min_segment
        )
    edges = [0, *points, n]
    means = [float(np.mean(series.y[a:b])) for a, b in pairwise(edges)] if points else []
    found = tuple(
        ChangePoint(
            index=index,
            time=float(series.t[index]),
            previous_time=float(series.t[index - 1]),
            mean_before=means[i],
            mean_after=means[i + 1],
            shift=means[i + 1] - means[i],
            shift_sigmas=(means[i + 1] - means[i]) / noise if noise > 0 else None,
        )
        for i, index in enumerate(points)
    )
    return ChangePointResult(
        change_points=found,
        searched=n >= 2 * min_segment,
        sigma=noise,
        penalty=beta,
        min_segment=min_segment,
        cost=cost,
        inputs=series.summary(),
    )


def detect_changes(
    daily: Iterable[DailyValue],
    *,
    metric_key: str,
    window_end: date,
    window_days: int,
    purpose: Purpose = Purpose.TREND,
    requirement: Requirement | None = None,
    penalty: float | None = None,
    min_segment: int = DEFAULT_MIN_SEGMENT,
    sigma: float | None = None,
) -> ChangePointResult | InsufficientData:
    """The change points of the metric's daily values inside the window, **only if** they pass
    the minimum-data policy (``purpose``'s requirement of the metric, or ``requirement``);
    otherwise the policy's ``InsufficientData``, with its ``n``, coverage and shortfalls and no
    search run. Days outside the window are ignored."""
    values = list(daily)
    if requirement is None:
        verdict = evaluate_metric(
            metric_key, purpose, values, window_end=window_end, window_days=window_days
        )
    else:
        verdict = evaluate(
            values, requirement=requirement, window_end=window_end, window_days=window_days
        )
    if isinstance(verdict, InsufficientData):
        return verdict
    window_start = window_end - timedelta(days=window_days - 1)
    inside = [v for v in values if window_start <= v.day <= window_end]
    found = find_changes(
        Series.from_daily(inside, window_days=window_days),
        penalty=penalty,
        min_segment=min_segment,
        sigma=sigma,
    )
    return ChangePointResult(
        change_points=found.change_points,
        searched=found.searched,
        sigma=found.sigma,
        penalty=found.penalty,
        min_segment=found.min_segment,
        cost=found.cost,
        inputs=found.inputs,
        data_quality=verdict.data_quality,
    )
