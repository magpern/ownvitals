"""Per-window signal-quality indices of the gate (ADR 0006 section 7).

The thresholds of the table in ``ownvitals.ecg.sqi`` and the functions that compute one index
of one window. ``sqi`` assembles them into the gate and re-exports every name here.

Pure: numpy and scipy only. This is a gate module: a change that can alter an output bumps
``ALGO_VERSION`` in ``sqi``.
"""

import numpy as np
from numpy.typing import NDArray
from scipy.signal import get_window

from ownvitals.ecg.input import to_samples
from ownvitals.ecg.rpeaks import matched_flags

BSQI_MIN = 0.90
RATE_MIN_BPM, RATE_MAX_BPM = 30.0, 220.0
GAP_MAX_S = 3.0
TSQI_MIN = 0.66
AMPLITUDE_MIN_MV, AMPLITUDE_MAX_MV = 0.1, 10.0
CLIP_MS = 20
WELCH_SEGMENT_S = 4

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
