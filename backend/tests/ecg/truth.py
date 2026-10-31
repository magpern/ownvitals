"""Ground truth and scoring for the synthetic strips: an independent restatement of ADR 0006
section 8 and of the beat matching of the test specification (ANSI/AAMI EC57).

SYNTHETIC: computed from the generator's R-events. Nothing here imports the code under test, so
a mistake in ``ownvitals.ecg.hrv`` cannot hide in its own reference.
"""

from dataclasses import dataclass

import numpy as np
from ecgsynth import Strip
from numpy.typing import NDArray

SYNTHETIC = True

MATCH_S = 0.150  # EC57: a detection within +-150 ms of a true beat matches it


@dataclass(frozen=True)
class Matching:
    tp: int
    fn: int
    fp: int
    pairs: tuple[tuple[int, int], ...]  # (index into truth, index into detections)

    @property
    def se(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def ppv(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0


def match_beats(
    detected_s: NDArray[np.float64], truth_s: NDArray[np.float64], tolerance: float = MATCH_S
) -> Matching:
    """One-to-one matching within the tolerance, nearest first."""
    taken: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for i, t in enumerate(truth_s):
        if detected_s.shape[0] == 0:
            break
        order = np.argsort(np.abs(detected_s - t))
        for j in order:
            if abs(detected_s[j] - t) > tolerance:
                break
            if int(j) not in taken:
                taken.add(int(j))
                pairs.append((i, int(j)))
                break
    tp = len(pairs)
    return Matching(tp, truth_s.shape[0] - tp, detected_s.shape[0] - tp, tuple(pairs))


def truth_in_span(strip: Strip, detected_s: NDArray[np.float64]) -> slice:
    """The true beats of a span: from the beat matching its first detection to the one
    matching its last (a tolerance of 150 ms at both ends)."""
    low, high = detected_s[0] - MATCH_S, detected_s[-1] + MATCH_S
    inside = np.flatnonzero((strip.r_times_s >= low) & (strip.r_times_s <= high))
    return slice(int(inside[0]), int(inside[-1]) + 1)


def score_span(strip: Strip, detected_s: NDArray[np.float64]) -> Matching:
    """Se and +P over the beats of the accepted span only."""
    return match_beats(detected_s, strip.r_times_s[truth_in_span(strip, detected_s)])


def rr_error_rms_ms(
    detected_s: NDArray[np.float64], truth_s: NDArray[np.float64], matching: Matching
) -> float:
    """RMS of (detected RR - true RR) over consecutive matched beats, in ms."""
    errors = []
    for (i0, j0), (i1, j1) in zip(matching.pairs, matching.pairs[1:], strict=False):
        if i1 == i0 + 1 and j1 == j0 + 1:
            errors.append(1000.0 * ((detected_s[j1] - detected_s[j0]) - (truth_s[i1] - truth_s[i0])))
    return float(np.sqrt(np.mean(np.square(errors)))) if errors else 0.0


@dataclass(frozen=True)
class Truth:
    """The reference values of one span: section 8 applied to the generator's sinus intervals."""

    hr_bpm: float
    sdnn_ms: float
    rmssd_ms: float
    pnn50_pct: float
    n_pairs: int
    band: int  # true pairs with |dNN| within 50 +- 10 ms
    nn_ms: NDArray[np.float64]
    sinus_interval: NDArray[np.bool_]  # per interval: both beats sinus


def truth_values(times_s: NDArray[np.float64], types: tuple[str, ...]) -> Truth:
    """HR from the intervals within 272.7-2,000 ms (ectopic beats included); SDNN, RMSSD and
    pNN50 from the sinus intervals (both beats sinus) over adjacent sinus pairs, as section 8 defines them."""
    rr = np.diff(times_s) * 1000.0
    in_range = (rr >= 60_000.0 / 220.0) & (rr <= 2_000.0)
    sinus = np.array([a == "N" and b == "N" for a, b in zip(types, types[1:], strict=False)])
    nn = rr[sinus]
    pairs = np.flatnonzero(sinus[:-1] & sinus[1:])
    steps = rr[pairs + 1] - rr[pairs]
    return Truth(
        hr_bpm=60_000.0 / float(np.mean(rr[in_range])),
        sdnn_ms=float(np.std(nn, ddof=1)),
        rmssd_ms=float(np.sqrt(np.mean(steps**2))),
        pnn50_pct=100.0 * float(np.mean(np.abs(steps) > 50.0)),
        n_pairs=int(pairs.shape[0]),
        band=int(np.sum(np.abs(np.abs(steps) - 50.0) <= 10.0)),
        nn_ms=nn,
        sinus_interval=sinus,
    )


def expected_exclusions(rr_ms: NDArray[np.float64]) -> list[int]:
    """Section 8's rules on a list of true intervals: the indices excluded by (a) or (b)."""
    reference = float(np.median(rr_ms[:5]))
    excluded = []
    for i, rr in enumerate(rr_ms):
        if not 60_000.0 / 220.0 <= rr <= 2_000.0 or abs(rr - reference) > 0.2 * reference:
            excluded.append(i)
        else:
            reference = float(rr)
    return excluded
