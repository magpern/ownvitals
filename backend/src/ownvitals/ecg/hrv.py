"""RR intervals, artefact handling, HR and HRV (ADR 0006 section 8).

Input: the refined R-peaks of the beat span that passed the gate, as sample positions.

* RR = differences of consecutive R-peaks, in ms.
* Exclusions, in order: (0) an interval across a dropped, unconfirmed beat (the gate drops
  every primary detection that the secondary detector does not confirm), ``unconfirmed_beat``;
  (a) outside 272.7-2,000 ms (30-220 bpm), ``rr_out_of_range``; (c) an interval touching a
  distorted beat (the gate finds those: a beat whose correlation with the median beat of its
  window is below 0.85), ``distorted_beat``; (b) more than 20 % from the last accepted interval,
  ``rr_deviation``, the reference starting at the median of the first five intervals that are
  neither (0), (c) nor (d). Excluded intervals are deleted, not interpolated.
  (d) ``missed_beat``, after (a) and before (c): an interval of at least 1.75 times the median
  of its up to eight neighbouring intervals (four each side, bridging intervals left out) is a
  beat the detector missed, not a pause. It applies only to a strip that is not AF-like, that
  is, one whose other exclusions are at most 20 % of the intervals (an irregular rhythm has no
  neighbouring rhythm to compare with). A compensatory pause is 1.2-1.4 times its neighbours.
* HR = 60,000 / the mean of the intervals left after (0), (a) and (d), ectopic beats and
  distorted beats included: (c) leaves the NN intervals only. **On an irregular rhythm HR is
  null**: when the intervals excluded by (0), (a), (c) and (b) are more than 20 % of all, the
  strip is AF-like, rule (d) has no neighbouring rhythm to compare with and a missed beat would
  corrupt the mean, so ``hr_bpm`` is ``None`` and ``hrv_reasons`` ends with ``irregular_rhythm``
  (the device's own HR is the value to show).
* HRV on the accepted (NN) intervals: SDNN with n - 1; RMSSD over adjacent NN pairs only (both
  accepted, sharing a beat); pNN50 = 100 * (pairs with |dNN| > 50 ms) / pairs.
* HRV needs at least 15 NN intervals, 10 pairs and an excluded fraction of at most 20 %.

Version 1 does not say whether an excluded interval is ectopic or an artefact: that would be
interpretation (ADR 0006 section 12). Rounding: HR, RMSSD, SDNN and pNN50 to 0.1.

Pure: numpy only. A change that can alter an output bumps ``ALGO_VERSION`` (ADR 0006 section
15).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

ALGO_VERSION = "5.0.0"

RR_MIN_MS = 60_000.0 / 220.0
RR_MAX_MS = 2_000.0
MAX_DEVIATION = 0.20
MISSED_RATIO = 1.75
MISSED_NEIGHBOURS = 4  # each side
MISSED_MIN_NEIGHBOURS = 3
REFERENCE_INTERVALS = 5
MIN_NN, MIN_PAIRS = 15, 10
MAX_EXCLUDED_FRACTION = 0.20
PNN_THRESHOLD_MS = 50.0


class Excluded(NamedTuple):
    """Interval ``i`` runs from R-peak ``i`` to R-peak ``i + 1`` of the span."""

    i: int
    reason: str  # "unconfirmed_beat", "rr_out_of_range", "missed_beat", "distorted_beat" or
    # "rr_deviation"


@dataclass(frozen=True)
class Hrv:
    rmssd_ms: float
    sdnn_ms: float
    pnn50_pct: float
    n_nn: int
    n_pairs: int
    excluded_fraction: float


@dataclass(frozen=True)
class RrAnalysis:
    """HR and, when the minimums hold, HRV; otherwise ``hrv`` is ``None`` with its reasons
    (``too_many_excluded`` before ``insufficient_nn``; ``irregular_rhythm`` last, with ``hr_bpm``
    null)."""

    n_rr: int
    excluded: tuple[Excluded, ...]
    hr_bpm: float | None
    hrv: Hrv | None
    hrv_reasons: tuple[str, ...]


def classify(
    rr_ms: NDArray[np.float64],
    unconfirmed: frozenset[int] = frozenset(),
    distorted: frozenset[int] = frozenset(),
    missed: frozenset[int] = frozenset(),
) -> list[str | None]:
    """The exclusion reason of each interval, ``None`` for an accepted (NN) interval."""
    reasons: list[str | None] = [None] * rr_ms.shape[0]
    usable = [float(rr) for i, rr in enumerate(rr_ms) if i not in unconfirmed | distorted | missed]
    reference = float(np.median(usable[:REFERENCE_INTERVALS])) if usable else 0.0
    for i, rr in enumerate(rr_ms):
        if i in unconfirmed:
            reasons[i] = "unconfirmed_beat"
        elif not RR_MIN_MS <= rr <= RR_MAX_MS:
            reasons[i] = "rr_out_of_range"
        elif i in missed:
            reasons[i] = "missed_beat"
        elif i in distorted:
            reasons[i] = "distorted_beat"
        elif abs(rr - reference) > MAX_DEVIATION * reference:
            reasons[i] = "rr_deviation"
        else:
            reference = float(rr)
    return reasons


def missed_beats(rr_ms: NDArray[np.float64], unconfirmed: frozenset[int]) -> frozenset[int]:
    """The intervals that are at least ``MISSED_RATIO`` times the median of their neighbours."""
    found: set[int] = set()
    for i, rr in enumerate(rr_ms):
        if i in unconfirmed or not RR_MIN_MS <= rr <= RR_MAX_MS:
            continue
        near = [
            float(rr_ms[j])
            for j in range(
                max(i - MISSED_NEIGHBOURS, 0), min(i + MISSED_NEIGHBOURS + 1, len(rr_ms))
            )
            if j != i and j not in unconfirmed
        ]
        if len(near) >= MISSED_MIN_NEIGHBOURS and rr >= MISSED_RATIO * float(np.median(near)):
            found.add(i)
    return frozenset(found)


def hrv_metrics(
    rr_ms: NDArray[np.float64], reasons: list[str | None]
) -> tuple[Hrv | None, tuple[str, ...]]:
    """HRV from the NN intervals, or ``None`` with the reasons it is not computed."""
    accepted = np.array([r is None for r in reasons], dtype=bool)
    n_all = rr_ms.shape[0]
    n_nn = int(accepted.sum())
    pairs = np.flatnonzero(accepted[:-1] & accepted[1:])
    fraction = (n_all - n_nn) / n_all if n_all else 0.0
    failing: list[str] = []
    if fraction > MAX_EXCLUDED_FRACTION:
        failing.append("too_many_excluded")
    if n_nn < MIN_NN or pairs.shape[0] < MIN_PAIRS:
        failing.append("insufficient_nn")
    if failing:
        return None, tuple(failing)
    steps = rr_ms[pairs + 1] - rr_ms[pairs]
    return (
        Hrv(
            rmssd_ms=round(float(np.sqrt(np.mean(steps**2))), 1),
            sdnn_ms=round(float(np.std(rr_ms[accepted], ddof=1)), 1),
            pnn50_pct=round(100.0 * float(np.mean(np.abs(steps) > PNN_THRESHOLD_MS)), 1),
            n_nn=n_nn,
            n_pairs=int(pairs.shape[0]),
            excluded_fraction=round(fraction, 4),
        ),
        (),
    )


def analyse(
    rpeaks: NDArray[np.float64],
    sampling_hz: int,
    unconfirmed: Iterable[int] = (),
    distorted: Iterable[int] = (),
) -> RrAnalysis:
    """HR and HRV of the R-peaks (sample positions) of one beat span; ``unconfirmed`` are the
    indices of the intervals that bridge a dropped beat and ``distorted`` the indices of the
    beats the gate found distorted (the intervals on both sides of each leave the NN)."""
    rr_ms = np.diff(rpeaks) * (1000.0 / sampling_hz)
    bridging = frozenset(unconfirmed)
    touching = frozenset(
        i for beat in distorted for i in (beat - 1, beat) if 0 <= i < rr_ms.shape[0]
    )
    reasons = classify(rr_ms, bridging, touching)
    missed: frozenset[int] = frozenset()
    irregular = sum(r is not None for r in reasons) > MAX_EXCLUDED_FRACTION * len(rr_ms)
    if rr_ms.shape[0] and not irregular:
        missed = missed_beats(rr_ms, bridging)
        if missed:
            reasons = classify(rr_ms, bridging, touching, missed)
    in_range = (rr_ms >= RR_MIN_MS) & (rr_ms <= RR_MAX_MS)
    in_range[sorted(bridging | missed)] = False
    hr = round(60_000.0 / float(np.mean(rr_ms[in_range])), 1) if in_range.any() else None
    hrv, hrv_reasons = hrv_metrics(rr_ms, reasons)
    if irregular:
        hr, hrv_reasons = None, (*hrv_reasons, "irregular_rhythm")
    return RrAnalysis(
        n_rr=int(rr_ms.shape[0]),
        excluded=tuple(Excluded(i, r) for i, r in enumerate(reasons) if r is not None),
        hr_bpm=hr,
        hrv=hrv,
        hrv_reasons=hrv_reasons,
    )
