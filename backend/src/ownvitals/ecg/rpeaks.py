"""R-peak detection (ADR 0006 section 6).

Two independent detectors run on the conditioned signal: Pan & Tompkins (1985), the primary,
with its published rule set, and Elgendi (2013), the secondary, used only for the agreement
index of the quality gate. Both are offline and centred, so no delay separates a detection
from its QRS, and both are made independent of the sampling rate by stating their windows in
milliseconds.

A detection is only a place to look. The R-peak itself is the same for both: the recording's
polarity is the sign of the largest deflection of the median beat, and each R-peak is the
argmax of polarity times the signal within +-75 ms of the detection, refined to a sub-sample
position by three-point parabolic interpolation. A beat whose +-75 ms window leaves the strip
is dropped. That makes the result independent of lead identity and inversion (A2).

Pure: numpy and scipy only. A change that can alter an output bumps ``ALGO_VERSION`` (ADR 0006
section 15); this is a gate module.
"""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import NDArray
from scipy.ndimage import maximum_filter1d, uniform_filter1d
from scipy.signal import butter, find_peaks

from ownvitals.ecg.filters import zero_phase
from ownvitals.ecg.input import to_samples

ALGO_VERSION = "2.0.0"

# Pan & Tompkins
PT_BAND_HZ, PT_BAND_ORDER = (5.0, 15.0), 2
PT_WINDOW_MS = 150
REFRACTORY_MS = 200
T_WAVE_MS = 360
LEARNING_S = 2.0
INIT_SIGNAL, INIT_NOISE = (
    0.75,
    0.5,
)  # of the maximum and of the mean of the learning phase (our choice)
SEARCH_BACK = 1.66  # of the average regular RR
REGULAR_LOW, REGULAR_HIGH = 0.92, 1.16  # of the average regular RR
RR_HISTORY = 8
MIN_INTERVALS = 3  # intervals before the rate can be called irregular or a beat missed
# Elgendi
EL_BAND_HZ, EL_BAND_ORDER = (8.0, 20.0), 3  # the order is our choice
EL_QRS_MS, EL_BEAT_MS, EL_BETA = 97, 611, 0.08
# Fiducial point
FIDUCIAL_MS = 75
# Agreement of the two detectors: bSQI and the confirmation of a beat (sections 6, 7)
MATCH_MS = 50

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]


BoolArray = NDArray[np.bool_]


@dataclass(frozen=True)
class Detection:
    """The R-peaks of both detectors, as (fractional) sample positions, the polarity, and which
    primary beats the secondary detector confirms (a match within +-50 ms)."""

    primary: FloatArray
    secondary: FloatArray
    polarity: int
    confirmed: BoolArray


def _odd(width: int) -> int:
    return width if width % 2 == 1 else width + 1


@lru_cache(maxsize=16)
def _bandpass_sos(band: tuple[float, float], order: int, fs: int) -> FloatArray:
    return np.asarray(butter(order, band, btype="bandpass", fs=fs, output="sos"), dtype=np.float64)


def _bandpass(y: FloatArray, band: tuple[float, float], order: int, fs: int) -> FloatArray:
    return zero_phase(_bandpass_sos(band, order, fs).copy(), y, fs)


@dataclass
class _Levels:
    """Signal and noise peak levels of one signal (the integrated or the band-passed one)."""

    signal: float
    noise: float

    def threshold(self) -> float:
        return self.noise + 0.25 * (self.signal - self.noise)

    def update(self, peak: float, *, signal: bool, search_back: bool) -> None:
        weight = 0.25 if search_back else 0.125
        if signal:
            self.signal = weight * peak + (1.0 - weight) * self.signal
        else:
            self.noise = weight * peak + (1.0 - weight) * self.noise


def pan_tompkins(y: FloatArray, fs: int) -> IntArray:
    """Sample positions of the QRS complexes, from the centred integrated signal."""
    band = _bandpass(y, PT_BAND_HZ, PT_BAND_ORDER, fs)
    derivative = np.correlate(band, np.array([-1.0, -2.0, 0.0, 2.0, 1.0]), mode="same")
    derivative *= fs / 8.0
    integrated = uniform_filter1d(
        derivative**2, size=_odd(to_samples(PT_WINDOW_MS, fs)), mode="nearest"
    )
    peaks, _ = find_peaks(integrated, distance=to_samples(REFRACTORY_MS, fs))
    if peaks.size == 0:
        return np.zeros(0, dtype=np.int64)
    size = 2 * to_samples(FIDUCIAL_MS, fs) + 1
    band_peak = maximum_filter1d(np.abs(band), size=size, mode="nearest")[peaks]
    slope = maximum_filter1d(np.abs(derivative), size=size, mode="nearest")[peaks]
    value = integrated[peaks]

    learn = int(LEARNING_S * fs)
    integral = _Levels(
        INIT_SIGNAL * float(np.max(integrated[:learn])),
        INIT_NOISE * float(np.mean(integrated[:learn])),
    )
    filtered = _Levels(
        INIT_SIGNAL * float(np.max(np.abs(band[:learn]))),
        INIT_NOISE * float(np.mean(np.abs(band[:learn]))),
    )
    found: list[int] = []  # indices into peaks
    intervals: list[float] = []
    regular: list[float] = []
    refractory = to_samples(REFRACTORY_MS, fs)
    t_wave = to_samples(T_WAVE_MS, fs)

    def average() -> float | None:
        recent = regular[-RR_HISTORY:]
        return sum(recent) / len(recent) if len(regular) >= MIN_INTERVALS else None

    def irregular() -> bool:
        mean = average()
        if mean is None or len(intervals) < MIN_INTERVALS:
            return False
        recent = intervals[-RR_HISTORY:]
        return any(not REGULAR_LOW * mean <= rr <= REGULAR_HIGH * mean for rr in recent)

    def accept(k: int, *, search_back: bool) -> None:
        if found:
            rr = float(peaks[k] - peaks[found[-1]])
            mean = average()
            intervals.append(rr)
            if mean is None or REGULAR_LOW * mean <= rr <= REGULAR_HIGH * mean:
                regular.append(rr)
        found.append(k)
        integral.update(float(value[k]), signal=True, search_back=search_back)
        filtered.update(float(band_peak[k]), signal=True, search_back=search_back)

    def reject(k: int) -> None:
        integral.update(float(value[k]), signal=False, search_back=False)
        filtered.update(float(band_peak[k]), signal=False, search_back=False)

    def thresholds() -> tuple[float, float, float, float]:
        i1, f1 = integral.threshold(), filtered.threshold()
        i2, f2 = 0.5 * i1, 0.5 * f1
        if irregular():
            i1, f1 = 0.5 * i1, 0.5 * f1
        return i1, i2, f1, f2

    def look_back(k: int, i2: float, f2: float) -> int | None:
        """The strongest candidate between the last QRS and candidate ``k``, if any."""
        best: int | None = None
        for j in range(found[-1] + 1, k):
            if peaks[j] - peaks[found[-1]] < refractory or value[j] <= i2 or band_peak[j] <= f2:
                continue
            if peaks[j] - peaks[found[-1]] < t_wave and slope[j] < 0.5 * slope[found[-1]]:
                continue
            if best is None or value[j] > value[best]:
                best = j
        return best

    for k in range(peaks.size):
        while (
            found
            and (mean := average()) is not None
            and peaks[k] - peaks[found[-1]] > (SEARCH_BACK * mean)
        ):
            i1, i2, f1, f2 = thresholds()
            missed = look_back(k, i2, f2)
            if missed is None:
                break
            accept(missed, search_back=True)
        i1, i2, f1, f2 = thresholds()
        if value[k] > i1 and band_peak[k] > f1:
            if found and peaks[k] - peaks[found[-1]] < t_wave and slope[k] < 0.5 * slope[found[-1]]:
                reject(k)  # a T wave
            else:
                accept(k, search_back=False)
        else:
            reject(k)
    return peaks[found].astype(np.int64)


def elgendi(y: FloatArray, fs: int) -> IntArray:
    """Sample positions of the QRS complexes: the maxima of the blocks of interest."""
    band = _bandpass(y, EL_BAND_HZ, EL_BAND_ORDER, fs)
    squared = band**2
    w_qrs = to_samples(EL_QRS_MS, fs)
    w_beat = _odd(to_samples(EL_BEAT_MS, fs))
    average_qrs = uniform_filter1d(squared, size=_odd(w_qrs), mode="nearest")
    average_beat = uniform_filter1d(squared, size=w_beat, mode="nearest")
    interest = average_qrs > average_beat + EL_BETA * float(np.mean(squared))
    edges = np.diff(np.concatenate(([0], interest.astype(np.int8), [0])))
    starts, stops = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    detections = [
        int(start + np.argmax(np.abs(band[start:stop])))
        for start, stop in zip(starts, stops, strict=True)
        if stop - start >= w_qrs
    ]
    return np.array(detections, dtype=np.int64)


def polarity_of(y: FloatArray, detections: IntArray, fs: int) -> int:
    """+1 or -1: the sign of the largest deflection of the median beat (+-75 ms)."""
    half = to_samples(FIDUCIAL_MS, fs)
    usable = [int(d) for d in detections if half <= d < y.shape[0] - half]
    if not usable:
        return 1
    median_beat = np.median(np.stack([y[d - half : d + half + 1] for d in usable]), axis=0)
    return 1 if median_beat[int(np.argmax(np.abs(median_beat)))] >= 0.0 else -1


def refine(y: FloatArray, detections: IntArray, polarity: int, fs: int) -> FloatArray:
    """The R-peak of each detection: the argmax of ``polarity * y`` within +-75 ms, then a
    parabola through three points. Detections whose window leaves the strip are dropped."""
    half = to_samples(FIDUCIAL_MS, fs)
    positions: list[float] = []
    for d in detections:
        lo, hi = int(d) - half, int(d) + half + 1
        if lo < 0 or hi > y.shape[0]:
            continue
        window = polarity * y[lo:hi]
        i = int(np.argmax(window))
        offset = 0.0
        if 0 < i < window.shape[0] - 1:
            below, top, above = window[i - 1], window[i], window[i + 1]
            curvature = below - 2.0 * top + above
            if curvature < 0.0:
                offset = float(np.clip(0.5 * (below - above) / curvature, -0.5, 0.5))
        positions.append(lo + i + offset)
    return np.unique(np.array(positions, dtype=np.float64))


def matched_flags(a: FloatArray, b: FloatArray, tolerance: float) -> BoolArray:
    """For each position of ``a`` (sorted): whether a position of ``b`` within the tolerance
    (inclusive) is matched to it. One-to-one, nearest first; the same matching as bSQI."""
    taken: set[int] = set()
    flags = np.zeros(a.shape[0], dtype=bool)
    for k, x in enumerate(a):
        j = int(np.searchsorted(b, x))
        near = [i for i in (j - 1, j) if 0 <= i < b.shape[0] and i not in taken]
        if not near:
            continue
        best = min(near, key=lambda i: abs(b[i] - x))
        if abs(b[best] - x) <= tolerance:
            taken.add(best)
            flags[k] = True
    return flags


def detect(y: FloatArray, fs: int) -> Detection:
    """Run both detectors on the conditioned signal ``y`` and fix the fiducial point."""
    first_pass = pan_tompkins(y, fs)
    second_pass = elgendi(y, fs)
    polarity = polarity_of(y, first_pass if first_pass.size else second_pass, fs)
    primary = refine(y, first_pass, polarity, fs)
    secondary = refine(y, second_pass, polarity, fs)
    return Detection(
        primary=primary,
        secondary=secondary,
        polarity=polarity,
        confirmed=matched_flags(primary, secondary, MATCH_MS * fs / 1000.0),
    )
