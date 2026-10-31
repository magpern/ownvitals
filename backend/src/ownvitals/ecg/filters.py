"""Conditioning filters (ADR 0006 section 5).

After the median is subtracted, one cascade of second-order sections runs forward and backward
(zero phase): a Butterworth high-pass (order 2, 0.5 Hz), notches at 50 and 60 Hz (Q = 30) and a
Butterworth low-pass (order 4, 40 Hz). Each section is designed per sampling rate, and the
``tf`` form is never used: it is numerically unsafe at 0.5 Hz.

Pure: numpy and scipy only. A change that can alter an output bumps ``ALGO_VERSION`` (ADR 0006
section 15); this is a gate module.
"""

from functools import lru_cache

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.signal import butter, iirnotch, sosfiltfilt, sosfreqz, tf2sos

ALGO_VERSION = "1.0.0"

HIGHPASS_HZ, HIGHPASS_ORDER = 0.5, 2
LOWPASS_HZ, LOWPASS_ORDER = 40.0, 4
NOTCH_HZ, NOTCH_Q = (50.0, 60.0), 30.0
PAD_S = 2.0  # padding of each pass, in seconds (capped at the strip's length)
PAD_TYPE = "odd"


@lru_cache(maxsize=8)
def _design(sampling_hz: float) -> NDArray[np.float64]:
    high = butter(HIGHPASS_ORDER, HIGHPASS_HZ, btype="highpass", fs=sampling_hz, output="sos")
    notches = [tf2sos(*iirnotch(f0, NOTCH_Q, fs=sampling_hz)) for f0 in NOTCH_HZ]
    low = butter(LOWPASS_ORDER, LOWPASS_HZ, btype="lowpass", fs=sampling_hz, output="sos")
    return np.vstack([high, *notches, low])


def design(sampling_hz: float) -> NDArray[np.float64]:
    """The second-order sections of the cascade (one pass), for this sampling rate."""
    return _design(sampling_hz).copy()


def zero_phase(
    sos: NDArray[np.float64], x: NDArray[np.float64], sampling_hz: float
) -> NDArray[np.float64]:
    """Forward-backward filtering with the padding of section 5: ``min(2 s, n - 1)`` samples."""
    if x.shape[0] < 2:
        raise ValueError("a signal needs at least two samples to be filtered")
    padlen = min(int(PAD_S * sampling_hz), x.shape[0] - 1)
    return sosfiltfilt(sos, x, padtype=PAD_TYPE, padlen=padlen)


def condition(x_mv: ArrayLike, sampling_hz: float) -> NDArray[np.float64]:
    """Subtract the median, then apply the cascade forward and backward."""
    x = np.asarray(x_mv, dtype=np.float64)
    return zero_phase(design(sampling_hz), x - np.median(x), sampling_hz)


def response_db(sampling_hz: float, frequencies_hz: ArrayLike) -> NDArray[np.float64]:
    """The composed forward-backward magnitude in dB: twice the one-pass response, because the
    second pass applies the same magnitude."""
    freqs = np.asarray(frequencies_hz, dtype=np.float64)
    _, h = sosfreqz(design(sampling_hz), worN=freqs, fs=sampling_hz)
    return 40.0 * np.log10(np.abs(h))


__all__ = ["ALGO_VERSION", "condition", "design", "response_db", "zero_phase"]
