"""The input every statistical method takes: a time series of floats, and what a result says
about it. Pure (numpy only).

``Series`` holds times in **days** (a float, so one day is ``1.0`` and a time of day is a
fraction) and values as ``float64``. Build it from daily values (``Series.from_daily``), from
timestamps (``Series.from_instants``) or from arrays (``Series.from_arrays``). The conversion
from the exact ``Decimal`` of the quality/daily steps to ``float`` happens here, once, at the
boundary to numpy (see ``docs/dev/analysis-methods.md``).

Every method result carries an ``InputSummary`` (the inputs' ``n`` and ``coverage``) and the
``ALGO_VERSION`` of the module that produced it (PLAN §5, §7.1 step 6).
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from fractions import Fraction
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray

from ownvitals.analysis.daily import DailyValue

type FloatArray = NDArray[np.float64]

EPOCH = date(1970, 1, 1)
EPOCH_INSTANT = datetime(1970, 1, 1, tzinfo=UTC)

# 1 / Φ⁻¹(3/4): scales a median absolute deviation to the standard deviation of a normal
# distribution (Rousseeuw and Croux 1993).
MAD_SCALE = 1.482602218505602


def mad(values: FloatArray) -> float:
    """The raw (unscaled) median absolute deviation about the median. ``values`` is not empty."""
    centre = float(np.median(values))
    return float(np.median(np.abs(values - centre)))


@dataclass(frozen=True, slots=True)
class InputSummary:
    """What a result was computed from.

    ``n`` points on ``n_days`` distinct days (``floor`` of the time) of a window of
    ``window_days`` days; ``coverage = n_days / window_days``. The window is the one given to the
    ``Series`` (the period the finding is about, e.g. 90 days) or, when none was given, the days
    from the first to the last day with data, both counted (a window of days, not the elapsed span
    of the minimum-data policy, which counts one less).
    """

    n: int
    n_days: int
    window_days: int
    coverage: Fraction
    first: float | None
    last: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "n_days": self.n_days,
            "window_days": self.window_days,
            "coverage": round(float(self.coverage), 4),
            "first": self.first,
            "last": self.last,
        }


def _readonly(array: FloatArray) -> FloatArray:
    array = np.array(array, dtype=np.float64, copy=True)
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True, eq=False)
class Series:
    """Times ``t`` (days, non-decreasing) and values ``y``; both finite and read-only.

    Tied times are allowed here; the methods say what they do with them (``trend`` refuses
    them). ``window_days`` is the length of the window the series stands for, for ``coverage``.
    """

    t: FloatArray
    y: FloatArray
    window_days: int | None = None

    def __post_init__(self) -> None:
        t, y = _readonly(self.t), _readonly(self.y)
        if t.ndim != 1 or y.ndim != 1 or t.shape != y.shape:
            raise ValueError("t and y must be one-dimensional and of the same length")
        if not (np.isfinite(t).all() and np.isfinite(y).all()):
            raise ValueError("t and y must be finite (no NaN or infinity)")
        if t.size > 1 and bool((np.diff(t) < 0).any()):
            raise ValueError("times must be non-decreasing: use Series.from_arrays to sort")
        if self.window_days is not None and self.window_days < 1:
            raise ValueError("window_days must be at least 1")
        object.__setattr__(self, "t", t)
        object.__setattr__(self, "y", y)
        if self.window_days is not None and self.window_days < self.n_days:
            raise ValueError(
                f"window_days={self.window_days} is shorter than the {self.n_days} days with data"
            )

    @classmethod
    def from_arrays(cls, t: ArrayLike, y: ArrayLike, *, window_days: int | None = None) -> "Series":
        """Sorts by ``(t, y)``, so the result never depends on the order the points arrive in."""
        times = np.asarray(t, dtype=np.float64)
        values = np.asarray(y, dtype=np.float64)
        if times.shape != values.shape or times.ndim != 1:
            raise ValueError("t and y must be one-dimensional and of the same length")
        order = np.lexsort((values, times))
        return cls(times[order], values[order], window_days)

    @classmethod
    def from_daily(cls, daily: Iterable[DailyValue], *, window_days: int | None = None) -> "Series":
        """One point per ``DailyValue`` at its local day (days since 1970-01-01), the value
        converted from ``Decimal`` to ``float``."""
        points = [((v.day - EPOCH).days, float(v.value)) for v in daily]
        return cls.from_arrays(
            [p[0] for p in points], [p[1] for p in points], window_days=window_days
        )

    @classmethod
    def from_instants(
        cls, instants: Sequence[datetime], values: ArrayLike, *, window_days: int | None = None
    ) -> "Series":
        """Timezone-aware instants become days since 1970-01-01T00:00Z (a day is 86400 s)."""
        if any(i.tzinfo is None or i.utcoffset() is None for i in instants):
            raise ValueError("instants must be timezone-aware")
        times = [(i - EPOCH_INSTANT).total_seconds() / 86400.0 for i in instants]
        return cls.from_arrays(times, values, window_days=window_days)

    @property
    def n(self) -> int:
        return int(self.t.size)

    @property
    def n_days(self) -> int:
        """Distinct days (``floor`` of the time) with at least one point."""
        return int(np.unique(np.floor(self.t)).size)

    def summary(self) -> InputSummary:
        if self.n == 0:
            return InputSummary(0, 0, self.window_days or 0, Fraction(0), None, None)
        n_days = self.n_days
        window = self.window_days
        if window is None:  # first to last day with data, both counted
            window = int(math.floor(self.t[-1]) - math.floor(self.t[0])) + 1
        return InputSummary(
            n=self.n,
            n_days=n_days,
            window_days=window,
            coverage=Fraction(n_days, window),
            first=float(self.t[0]),
            last=float(self.t[-1]),
        )


def finite_or_none(value: float) -> float | None:
    """``value`` for JSON: NaN and infinity become ``None``."""
    return float(value) if math.isfinite(value) else None


def list_or_none(values: FloatArray) -> list[float | None]:
    return [finite_or_none(float(v)) for v in values]
