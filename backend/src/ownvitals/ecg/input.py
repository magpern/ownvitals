"""Input step of the ECG pipeline (ADR 0006 sections 4 and 7).

Checks the recording before any processing: a missing signal and a sampling rate the pipeline
does not analyse give no result at all (``NotAnalysed``, never a gate result). Otherwise the
first lead (A5) is converted from int32 microvolts to float64 millivolts, and the 10-s windows
of the quality gate are laid out.

Pure: numpy only, no I/O, no clock, no randomness. A change that can alter an output bumps
``ALGO_VERSION`` (ADR 0006 section 15); this is a gate module.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

ALGO_VERSION = "1.0.0"

SUPPORTED_RATES = (300, 500)  # Hz (A3); other rates are stored and shown but not analysed
WINDOW_S = 10  # quality window length (section 7)
STEP_S = 5  # quality window step; the last window is aligned to the strip's end
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1

NotAnalysedReason = Literal["signal_missing", "unsupported_sampling_rate"]


@dataclass(frozen=True)
class NotAnalysed:
    """The recording cannot be analysed. It has no gate result (ADR 0006 section 11)."""

    reason: NotAnalysedReason


@dataclass(frozen=True)
class Prepared:
    """The analysed lead: ``raw_uv`` as received (int64 holding int32 values) and ``x_mv``."""

    lead: int
    sampling_hz: int
    raw_uv: NDArray[np.int64]
    x_mv: NDArray[np.float64]

    @property
    def n_samples(self) -> int:
        return int(self.raw_uv.shape[0])


def to_samples(milliseconds: float, sampling_hz: float) -> int:
    """A duration in whole samples, rounded half up: 75 ms is 23 samples at 300 Hz."""
    return int(milliseconds * sampling_hz / 1000.0 + 0.5)


def window_bounds(n_samples: int, sampling_hz: int) -> list[tuple[int, int]]:
    """The ``(start, stop)`` sample bounds of the 10-s windows, 5 s apart; the last one is
    aligned to the end of the strip. A strip shorter than one window has none."""
    length = WINDOW_S * sampling_hz
    step = STEP_S * sampling_hz
    if n_samples < length:
        return []
    starts = list(range(0, n_samples - length + 1, step))
    if starts[-1] + length < n_samples:
        starts.append(n_samples - length)
    return [(start, start + length) for start in starts]


def _first_lead(leads: Sequence[Any] | None) -> NDArray[np.int64] | None:
    """The first lead as int64, ``None`` when there is none. Refuses (``ValueError``) anything
    but integers in the int32 range: ingest (#25) validates, so this is a programming error and
    nothing is coerced."""
    if leads is None or len(leads) == 0:
        return None
    lead = np.asarray(leads[0])
    if lead.ndim != 1:
        raise ValueError("a lead must be a one-dimensional array of integers")
    if lead.size == 0:
        return None
    if lead.dtype.kind not in "iu":
        raise ValueError("a lead must hold integers")
    if lead.min() < INT32_MIN or lead.max() > INT32_MAX:
        raise ValueError("a lead must hold int32 values")
    return lead.astype(np.int64)


def prepare(leads: Sequence[Any] | None, sampling_hz: int | None) -> Prepared | NotAnalysed:
    """Check the recording and convert the first lead to millivolts."""
    raw = _first_lead(leads)
    if raw is None:
        return NotAnalysed("signal_missing")
    if sampling_hz not in SUPPORTED_RATES:
        return NotAnalysed("unsupported_sampling_rate")
    return Prepared(
        lead=0,
        sampling_hz=int(sampling_hz),
        raw_uv=raw,
        x_mv=raw.astype(np.float64) / 1000.0,
    )
