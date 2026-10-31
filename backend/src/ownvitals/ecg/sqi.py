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
from scipy.signal import get_window

from ownvitals.ecg.input import Prepared, to_samples, window_bounds
from ownvitals.ecg.rpeaks import MATCH_MS, Detection, matched_flags

ALGO_VERSION = "3.0.0"

BSQI_MIN = 0.90
RATE_MIN_BPM, RATE_MAX_BPM = 30.0, 220.0
GAP_MAX_S = 3.0
TSQI_MIN = 0.66
AMPLITUDE_MIN_MV, AMPLITUDE_MAX_MV = 0.1, 10.0
CLIP_MS = 20
MIN_SPAN_S = 20.0
DISTORTED_R = 0.85  # a beat correlating below this with the median beat of its window
QRS_HALF_MS = 100  # the segment of that correlation: the beat's R-peak +- 100 ms
EDGE_TRIM_S = 5.0  # a distorted beat this close to an end of the span takes the beats before it out
WELCH_SEGMENT_S = 4
SETTLE_MS = 500  # edge of the strip left out of kSQI and pSQI: the filters have not settled

# The order of reasons in every result: `too_short` first, then the window reasons.
REASONS = (
    "too_short",
    "flat_line",
    "amplitude_out_of_range",
    "clipping",
    "detector_disagreement",
    "implausible_rate",
    "beat_gap",
    "low_template_correlation",
)

FloatArray = NDArray[np.float64]


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


def _long_runs(flag: NDArray[np.bool_], minimum: int) -> NDArray[np.bool_]:
    """True where ``flag`` is True in a run of at least ``minimum`` samples."""
    edges = np.diff(np.concatenate(([0], flag.astype(np.int8), [0])))
    mask = np.zeros(flag.shape[0], dtype=bool)
    for start, stop in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True):
        if stop - start >= minimum:
            mask[start:stop] = True
    return mask


def clipped_samples(raw_uv: NDArray[np.int64], sampling_hz: int) -> NDArray[np.bool_]:
    """Samples in a run of at least 20 ms of identical values at the strip's raw extremes."""
    minimum = to_samples(CLIP_MS, sampling_hz)
    return _long_runs(raw_uv == raw_uv.max(), minimum) | _long_runs(raw_uv == raw_uv.min(), minimum)


def agreement(a: FloatArray, b: FloatArray, tolerance: float) -> float:
    """``matched / (n_a + n_b - matched)``, one-to-one matching within the tolerance."""
    matched = int(matched_flags(a, b, tolerance).sum())
    union = a.shape[0] + b.shape[0] - matched
    return matched / union if union else 0.0


def longest_gap_s(beats: FloatArray, start: int, stop: int, sampling_hz: int) -> float:
    """The longest stretch without a beat in the window, its edges included."""
    edges = np.concatenate(([float(start)], beats, [float(stop)]))
    return float(np.max(np.diff(edges))) / sampling_hz


def rate_bpm(beats: FloatArray, sampling_hz: int) -> float | None:
    if beats.shape[0] < 2:
        return None
    return 60.0 * (beats.shape[0] - 1) / ((beats[-1] - beats[0]) / sampling_hz)


def beat_correlations(
    y: FloatArray, beats: FloatArray, start: int, stop: int, half: int | None = None
) -> FloatArray:
    """Pearson correlation of each beat with the median beat of the window, over a segment of
    ``2 half + 1`` samples centred on its R-peak (``half`` defaults to half the median RR).
    NaN for a beat whose segment leaves ``[start, stop)``, and for all beats when fewer than two
    fit."""
    result = np.full(beats.shape[0], np.nan)
    if beats.shape[0] < 2:
        return result
    if half is None:
        half = round(float(np.median(np.diff(beats))) / 2.0)
    centres = np.rint(beats).astype(np.int64)
    fits = (centres - half >= start) & (centres + half + 1 <= stop)
    if fits.sum() < 2 or half < 1:
        return result
    segments = np.stack([y[c - half : c + half + 1] for c in centres[fits]])
    template = np.median(segments, axis=0)
    segments = segments - segments.mean(axis=1, keepdims=True)
    template = template - template.mean()
    norm = np.linalg.norm(segments, axis=1) * np.linalg.norm(template)
    dot = segments @ template
    result[fits] = np.divide(dot, norm, out=np.zeros_like(dot), where=norm > 0.0)
    return result


def template_correlation(y: FloatArray, beats: FloatArray, start: int, stop: int) -> float | None:
    """tSQI: the mean of ``beat_correlations`` over segments of the median RR; fewer than two
    beats left means no index."""
    correlations = beat_correlations(y, beats, start, stop)
    finite = correlations[~np.isnan(correlations)]
    return float(np.mean(finite)) if finite.shape[0] >= 2 else None


def kurtosis(x: FloatArray) -> float | None:
    """Pearson kurtosis (3 for a Gaussian); ``None`` for a constant signal."""
    centred = x - x.mean()
    m2 = float(np.mean(centred**2))
    if m2 <= 0.0:
        return None
    return float(np.mean(centred**4)) / m2**2


def welch_density(x: FloatArray, sampling_hz: int) -> tuple[FloatArray, FloatArray]:
    """One-sided power spectral density by Welch's method: 4-s periodic Hann segments, 50 %
    overlap, on the window minus its mean and with no further detrending. The same numbers as
    ``scipy.signal.welch(x - x.mean(), fs, "hann", 4 s, 2 s, detrend=False)`` (the tests compare
    them), without its per-call overhead."""
    length = WELCH_SEGMENT_S * sampling_hz
    taper = np.asarray(get_window("hann", length), dtype=np.float64)
    centred = x - x.mean()
    starts = range(0, centred.shape[0] - length + 1, length // 2)
    segments = np.stack([centred[s : s + length] for s in starts]) * taper
    density = np.mean(np.abs(np.fft.rfft(segments, axis=1)) ** 2, axis=0)
    density /= sampling_hz * float(np.sum(taper**2))
    density[1:-1] *= 2.0  # one-sided; the DC and Nyquist bins appear once
    return np.fft.rfftfreq(length, 1.0 / sampling_hz), density


def _band_power(
    x: FloatArray, sampling_hz: int, bands: tuple[tuple[float, float], ...]
) -> list[float]:
    freqs, density = welch_density(x, sampling_hz)
    return [float(np.sum(density[(freqs >= lo) & (freqs <= hi)])) for lo, hi in bands]


def psqi(y: FloatArray, sampling_hz: int) -> float | None:
    """Power 5-15 Hz over power 5-40 Hz of the conditioned window."""
    inner, outer = _band_power(y, sampling_hz, ((5.0, 15.0), (5.0, 40.0)))
    return inner / outer if outer > 0.0 else None


def bassqi(x: FloatArray, sampling_hz: int) -> float | None:
    """1 minus the power 0-1 Hz over the power 0-40 Hz of the unfiltered window."""
    low, total = _band_power(x, sampling_hz, ((0.0, 1.0), (0.0, 40.0)))
    return 1.0 - low / total if total > 0.0 else None


def window_reasons(
    *,
    bsqi: float,
    rate: float | None,
    gap_s: float,
    tsqi: float | None,
    amplitude_mv: float,
    clipped: bool,
) -> tuple[str, ...]:
    """The reasons for the rows that fail, in the order of ``REASONS``."""
    failing = {
        "flat_line": amplitude_mv < AMPLITUDE_MIN_MV,
        "amplitude_out_of_range": amplitude_mv > AMPLITUDE_MAX_MV,
        "clipping": clipped,
        "detector_disagreement": bsqi < BSQI_MIN,
        "implausible_rate": rate is None or not RATE_MIN_BPM <= rate <= RATE_MAX_BPM,
        "beat_gap": gap_s > GAP_MAX_S,
        "low_template_correlation": tsqi is None or tsqi < TSQI_MIN,
    }
    return tuple(reason for reason in REASONS if failing.get(reason, False))


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
        bridging = tuple(i - first for i in bridging if first <= i < last)
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
