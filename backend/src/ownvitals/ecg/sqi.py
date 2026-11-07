"""Signal-quality indices and the gate (ADR 0006 section 7).

The strip is cut into 10-s windows, 5 s apart (the last aligned to the end). A window passes
when every row of the table holds:

=================  =======================================================  ==============
Index              Definition                                               Pass
=================  =======================================================  ==============
bSQI               detector agreement, matched within +-50 ms               >= 0.90
Rate               60 (n - 1) / (t_last - t_first)                          30 to 220 bpm
Gap                longest stretch without a beat, window edges included   <= 3.0 s
tSQI               mean correlation of each beat with the median beat       >= 0.66
Amplitude          99.5th - 0.5th percentile of the conditioned window      0.1 to 10 mV
Clipping           runs >= 20 ms at the strip's raw minimum or maximum      none
=================  =======================================================  ==============

kSQI, pSQI and basSQI are recorded for every window but do not gate. No index uses RR
regularity: a gate that rejected irregular rhythm would bias any later rhythm analysis.

The filter chain pads the strip with an odd extension (ADR 0006 section 5), which turns the
first and last sample into a step; the transient takes about 0.5 s to die out and inflates the
kurtosis of the windows at the strip's edges (white noise: up to 14 instead of 3). So the first
and the last window leave the 0.5 s at the strip's edge out of kSQI and pSQI. No gating index
changes, and nor does the gate decision.

The recording passes when its longest *beat span* (first to last R-peak of a run of consecutive
passing windows) is at least 20 s; only the beats of that span go downstream, and of
those only the ones the secondary detector confirms (a primary beat without an Elgendi beat
within +-50 ms is dropped, and the interval across it is not an RR interval).

The span is then *verified*. A confirmed beat is distorted when its Pearson correlation with
the median beat of its window, over its R-peak +-100 ms, is under ``DISTORTED_R``. The segment
has a fixed length, so that the neighbours of a premature beat do not count. A distorted beat
within ``EDGE_TRIM_S`` of an end of the span takes every beat between it and that end out (a
burst of movement at an end of a strip adds, hides and displaces beats), and the span is
measured again on the beats left: under 20 s the recording fails with ``too_short``. Distorted
beats in the interior stay in the span, and ``hrv`` excludes the intervals that touch them.

A failing recording is a typed ``QualityRejected`` that carries the reasons and the per-window
indices and nothing else: no R-peaks, no HR, no HRV.

Pure: numpy and scipy only. A change that can alter an output bumps ``ALGO_VERSION`` (ADR 0006
section 15); this is a gate module.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from ownvitals.ecg.input import Prepared, to_samples, window_bounds
from ownvitals.ecg.rpeaks import MATCH_MS, Detection
from ownvitals.ecg.sqi_indices import (
    AMPLITUDE_MAX_MV as AMPLITUDE_MAX_MV,
)
from ownvitals.ecg.sqi_indices import (
    AMPLITUDE_MIN_MV as AMPLITUDE_MIN_MV,
)
from ownvitals.ecg.sqi_indices import (
    BSQI_MIN as BSQI_MIN,
)
from ownvitals.ecg.sqi_indices import (
    CLIP_MS as CLIP_MS,
)
from ownvitals.ecg.sqi_indices import (
    GAP_MAX_S as GAP_MAX_S,
)
from ownvitals.ecg.sqi_indices import (
    RATE_MAX_BPM as RATE_MAX_BPM,
)
from ownvitals.ecg.sqi_indices import (
    RATE_MIN_BPM as RATE_MIN_BPM,
)
from ownvitals.ecg.sqi_indices import (
    REASONS,
    FloatArray,
    agreement,
    bassqi,
    beat_correlations,
    clipped_samples,
    kurtosis,
    longest_gap_s,
    psqi,
    rate_bpm,
    template_correlation,
    window_reasons,
)
from ownvitals.ecg.sqi_indices import (
    TSQI_MIN as TSQI_MIN,
)
from ownvitals.ecg.sqi_indices import (
    WELCH_SEGMENT_S as WELCH_SEGMENT_S,
)
from ownvitals.ecg.sqi_indices import welch_density as welch_density

ALGO_VERSION = "4.0.0"

MIN_SPAN_S = 20.0
DISTORTED_R = 0.85  # a beat correlating below this with the median beat of its window
QRS_HALF_MS = 100  # the segment of that correlation: the beat's R-peak +- 100 ms
EDGE_TRIM_S = 5.0  # a distorted beat this close to an end of the span takes the beats before it out
SETTLE_MS = 500  # edge of the strip left out of kSQI and pSQI: the filters have not settled


@dataclass(frozen=True)
class WindowIndices:
    """The indices of one window. ``None`` where an index is not defined (too few beats, no
    variance)."""

    sampling_hz: int
    start: int  # samples, inclusive
    stop: int  # samples, exclusive
    bsqi: float
    rate_bpm: float | None
    gap_s: float
    tsqi: float | None
    amplitude_mv: float
    clipped: bool
    ksqi: float | None
    psqi: float | None
    bassqi: float | None
    reasons: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class QualityRejected:
    """The strip failed the gate. By design this carries nothing derived from the beats."""

    reasons: tuple[str, ...]
    windows: tuple[WindowIndices, ...]


@dataclass(frozen=True)
class GatePassed:
    """The strip passed: the confirmed beats of the longest span go on to HR and HRV.

    ``rpeaks`` are the primary detector's beats of the span that the secondary one confirms
    (sample positions). ``unconfirmed`` lists the intervals ``i`` (from ``rpeaks[i]`` to
    ``rpeaks[i + 1]``) that bridge a dropped, unconfirmed beat: they are not real RR intervals.
    """

    windows: tuple[WindowIndices, ...]
    span_s: float
    rpeaks: FloatArray
    unconfirmed: tuple[int, ...]
    beat_r: FloatArray  # per kept beat: correlation with the median beat of its window (or NaN)
    distorted: tuple[int, ...]  # the kept beats (indices into rpeaks) below DISTORTED_R


def assess_window(
    start: int,
    stop: int,
    prepared: Prepared,
    y: FloatArray,
    detection: Detection,
    clipped: NDArray[np.bool_],
) -> WindowIndices:
    fs = prepared.sampling_hz
    settle = to_samples(SETTLE_MS, fs)
    primary = detection.primary[(detection.primary >= start) & (detection.primary < stop)]
    secondary = detection.secondary[(detection.secondary >= start) & (detection.secondary < stop)]
    window = y[start:stop]
    settled = y[
        start + (settle if start == 0 else 0) : stop - (settle if stop == prepared.n_samples else 0)
    ]
    low, high = np.percentile(window, [0.5, 99.5])
    bsqi = agreement(primary, secondary, MATCH_MS * fs / 1000.0)
    rate = rate_bpm(primary, fs)
    gap_s = longest_gap_s(primary, start, stop, fs)
    tsqi = template_correlation(y, primary, start, stop)
    amplitude = float(high - low)
    is_clipped = bool(np.any(clipped[start:stop]))
    return WindowIndices(
        sampling_hz=fs,
        start=start,
        stop=stop,
        bsqi=bsqi,
        rate_bpm=rate,
        gap_s=gap_s,
        tsqi=tsqi,
        amplitude_mv=amplitude,
        clipped=is_clipped,
        ksqi=kurtosis(settled),
        psqi=psqi(settled, fs),
        bassqi=bassqi(prepared.x_mv[start:stop], fs),
        reasons=window_reasons(
            bsqi=bsqi,
            rate=rate,
            gap_s=gap_s,
            tsqi=tsqi,
            amplitude_mv=amplitude,
            clipped=is_clipped,
        ),
    )


def _runs(passed: list[bool]) -> list[tuple[int, int]]:
    """``(first, last)`` window indices of each run of consecutive passing windows."""
    runs: list[tuple[int, int]] = []
    first: int | None = None
    for i, ok in enumerate([*passed, False]):
        if ok and first is None:
            first = i
        elif not ok and first is not None:
            runs.append((first, i - 1))
            first = None
    return runs


def longest_span(
    windows: tuple[WindowIndices, ...], detection: Detection, sampling_hz: int
) -> tuple[float, FloatArray, NDArray[np.bool_], tuple[WindowIndices, ...]]:
    """The longest beat span over the runs of passing windows: its length in s, its beats,
    whether the secondary detector confirms each, and the windows of the run."""
    best: tuple[float, FloatArray, NDArray[np.bool_], tuple[WindowIndices, ...]] = (
        0.0,
        np.zeros(0, dtype=np.float64),
        np.zeros(0, dtype=bool),
        (),
    )
    for first, last in _runs([w.passed for w in windows]):
        start, stop = windows[first].start, windows[last].stop
        inside = (detection.primary >= start) & (detection.primary < stop)
        beats = detection.primary[inside]
        span = float(beats[-1] - beats[0]) / sampling_hz if beats.shape[0] >= 2 else 0.0
        if span > best[0]:
            best = (span, beats, detection.confirmed[inside], windows[first : last + 1])
    return best


def span_correlations(
    y: FloatArray, beats: FloatArray, run: tuple[WindowIndices, ...]
) -> FloatArray:
    """The correlation of each beat of a span with the median beat of its window, over its
    R-peak +- ``QRS_HALF_MS``: of the windows of the run that contain the beat's segment, the one
    whose centre is nearest to the beat. A segment that leaves the strip is not assessed (NaN).
    The segment is a fixed length, so that the neighbours of a premature beat, or the beats of an
    irregular rhythm, are not mistaken for distorted ones: it holds the QRS and no other beat."""
    best = np.full(beats.shape[0], np.nan)
    distance = np.full(beats.shape[0], np.inf)
    half = to_samples(QRS_HALF_MS, run[0].sampling_hz)
    for window in run:
        inside = np.flatnonzero((beats >= window.start) & (beats < window.stop))
        r = beat_correlations(y, beats[inside], window.start, window.stop, half)
        away = np.abs(beats[inside] - (window.start + window.stop) / 2.0)
        better = ~np.isnan(r) & (away < distance[inside])
        best[inside[better]] = r[better]
        distance[inside[better]] = away[better]
    return best


def verified_range(beats: FloatArray, beat_r: FloatArray, sampling_hz: int) -> tuple[int, int]:
    """The first and last beat of a span left after the edges are trimmed: a distorted beat
    within ``EDGE_TRIM_S`` of an end of the span takes every beat between it and that end out.
    A burst of movement at the start or the end of a strip hides, adds and displaces beats;
    the beats on its far side are the ones whose timing is known."""
    seconds = beats / sampling_hz
    distorted = beat_r < DISTORTED_R  # an unassessed beat (NaN) is not distorted
    lead = np.flatnonzero(distorted & (seconds < seconds[0] + EDGE_TRIM_S))
    trail = np.flatnonzero(distorted & (seconds > seconds[-1] - EDGE_TRIM_S))
    first = int(lead[-1]) + 1 if lead.shape[0] else 0
    last = int(trail[0]) - 1 if trail.shape[0] else beats.shape[0] - 1
    return first, max(last, first)


def confirmed_beats(
    beats: FloatArray, confirmed: NDArray[np.bool_]
) -> tuple[FloatArray, tuple[int, ...]]:
    """Drop the unconfirmed beats. An interval between two kept beats that had a dropped beat
    between them is reported by its index; dropped beats at the ends bridge nothing."""
    kept: list[float] = []
    bridging: list[int] = []
    pending = False
    for position, ok in zip(beats, confirmed, strict=True):
        if ok:
            if kept and pending:
                bridging.append(len(kept) - 1)
            kept.append(float(position))
            pending = False
        elif kept:
            pending = True
    return np.array(kept, dtype=np.float64), tuple(bridging)


def trimmed_bridging(bridging: tuple[int, ...], first: int, last: int) -> tuple[int, ...]:
    """The bridging intervals (index ``i`` runs from kept beat ``i`` to ``i + 1``) that lie
    between beats ``first`` and ``last``, re-indexed from ``first``."""
    return tuple(i - first for i in bridging if first <= i < last)


def evaluate(
    prepared: Prepared, y: FloatArray, detection: Detection
) -> GatePassed | QualityRejected:
    """The gate: per-window indices, then the longest beat span."""
    bounds = window_bounds(prepared.n_samples, prepared.sampling_hz)
    clipped = clipped_samples(prepared.raw_uv, prepared.sampling_hz)
    windows = tuple(assess_window(a, b, prepared, y, detection, clipped) for a, b in bounds)
    span_s, beats, confirmed, run = longest_span(windows, detection, prepared.sampling_hz)
    if span_s >= MIN_SPAN_S:
        kept, bridging = confirmed_beats(beats, confirmed)
        beat_r = span_correlations(y, beats, run)[confirmed]
        first, last = verified_range(kept, beat_r, prepared.sampling_hz)
        kept, beat_r = kept[first : last + 1], beat_r[first : last + 1]
        bridging = trimmed_bridging(bridging, first, last)
        verified_s = float(kept[-1] - kept[0]) / prepared.sampling_hz if kept.shape[0] > 1 else 0.0
        if verified_s >= MIN_SPAN_S:
            return GatePassed(
                windows=windows,
                span_s=verified_s,
                rpeaks=kept,
                unconfirmed=bridging,
                beat_r=beat_r,
                distorted=tuple(int(i) for i in np.flatnonzero(beat_r < DISTORTED_R)),
            )
    failing = {reason for w in windows for reason in w.reasons}
    reasons = ("too_short", *(r for r in REASONS if r in failing))
    return QualityRejected(reasons=reasons, windows=windows)
