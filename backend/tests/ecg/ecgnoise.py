"""Noise and artefact models for the synthetic ECG strips (ADR 0006 section 13).

SYNTHETIC: everything here is invented. SNR follows the MIT-BIH noise stress test (Moody 1984):
``SNR = 10 log10((A_pp^2 / 8) / P_noise)`` with ``A_pp`` the median QRS peak-to-peak amplitude
of the clean strip and ``P_noise`` the power over the noise's active time.

Each model returns noise in mV (a float array as long as the strip) except the ones that change
the recorded samples themselves (dropout, clipping, flat line), which return a new strip.
"""

import numpy as np
from numpy.typing import NDArray
from scipy.signal import butter, sosfiltfilt

from ecgsynth import Strip, peak_to_peak_mv

SYNTHETIC = True


def noise_power(strip: Strip, snr_db: float) -> float:
    """``P_noise`` in mV^2 for the SNR."""
    return (peak_to_peak_mv(strip) ** 2 / 8.0) / 10.0 ** (snr_db / 10.0)


def white(rng: np.random.Generator, strip: Strip, snr_db: float) -> NDArray[np.float64]:
    return rng.standard_normal(strip.n_samples) * np.sqrt(noise_power(strip, snr_db))


def emg_like(rng: np.random.Generator, strip: Strip, snr_db: float) -> NDArray[np.float64]:
    """White noise high-passed at 20 Hz, scaled to the SNR."""
    sos = butter(4, 20.0, btype="highpass", fs=strip.sampling_hz, output="sos")
    raw = sosfiltfilt(sos, rng.standard_normal(strip.n_samples))
    return raw * np.sqrt(noise_power(strip, snr_db) / np.mean(raw**2))


def baseline_wander(rng: np.random.Generator, strip: Strip) -> NDArray[np.float64]:
    """0.3 Hz and 0.05 Hz sinusoids, each of amplitude A."""
    t = np.arange(strip.n_samples) / strip.sampling_hz
    p1, p2 = rng.uniform(0.0, 2.0 * np.pi, size=2)
    a = strip.amplitude_mv
    return a * np.sin(2.0 * np.pi * 0.3 * t + p1) + a * np.sin(2.0 * np.pi * 0.05 * t + p2)


def mains(rng: np.random.Generator, strip: Strip, nominal_hz: float) -> NDArray[np.float64]:
    """Mains at 50 or 60 Hz +- 0.2 Hz, 0.5 A, and a third harmonic of 0.1 A below Nyquist."""
    t = np.arange(strip.n_samples) / strip.sampling_hz
    frequency = nominal_hz + rng.uniform(-0.2, 0.2)
    p1, p3 = rng.uniform(0.0, 2.0 * np.pi, size=2)
    a = strip.amplitude_mv
    noise = 0.5 * a * np.sin(2.0 * np.pi * frequency * t + p1)
    if 3.0 * frequency < strip.sampling_hz / 2.0:
        noise += 0.1 * a * np.sin(2.0 * np.pi * 3.0 * frequency * t + p3)
    return noise


def motion_burst(
    rng: np.random.Generator, strip: Strip, start_s: float, length_s: float, snr_db: float
) -> NDArray[np.float64]:
    """One burst of noise band-passed 0.5-10 Hz with 50 ms tapers, power set over the burst."""
    fs = strip.sampling_hz
    first = round(start_s * fs)
    count = min(round(length_s * fs), strip.n_samples - first)
    sos = butter(2, [0.5, 10.0], btype="bandpass", fs=fs, output="sos")
    burst = sosfiltfilt(sos, rng.standard_normal(count))
    taper = round(0.05 * fs)
    ramp = np.sin(np.linspace(0.0, np.pi / 2.0, taper)) ** 2
    window = np.ones(count)
    window[:taper] = ramp
    window[-taper:] = ramp[::-1]
    burst *= window
    burst *= np.sqrt(noise_power(strip, snr_db) / np.mean(burst**2))
    noise = np.zeros(strip.n_samples)
    noise[first : first + count] = burst
    return noise


def motion_bursts(
    rng: np.random.Generator, strip: Strip, snr_db: float, coverage: float = 0.5
) -> NDArray[np.float64]:
    """Bursts of 1 to 3 s, spread over the strip, covering at least ``coverage`` of it."""
    duration = strip.n_samples / strip.sampling_hz
    while True:
        spans: list[tuple[float, float]] = []
        t = float(rng.uniform(0.0, 1.5))
        while t < duration - 1.0:
            length = float(min(rng.uniform(1.0, 3.0), duration - t))
            spans.append((t, length))
            t += length + float(rng.uniform(0.5, 2.0))
        if sum(length for _, length in spans) >= coverage * duration:
            break
    noise = np.zeros(strip.n_samples)
    for start, length in spans:
        noise += motion_burst(rng, strip, start, length, snr_db)
    return noise


def electrode_pop(strip: Strip, at_s: float) -> NDArray[np.float64]:
    """A step of 2 A that decays with tau = 0.3 s."""
    t = np.arange(strip.n_samples) / strip.sampling_hz - at_s
    return np.where(t >= 0.0, 2.0 * strip.amplitude_mv * np.exp(-np.maximum(t, 0.0) / 0.3), 0.0)


def noisy(strip: Strip, *noises: NDArray[np.float64]) -> Strip:
    """The strip with the noises added and rounded to int microvolts."""
    total = strip.clean_mv + sum(noises, np.zeros(strip.n_samples))
    return strip.with_signal(np.rint(total * 1000.0).astype(np.int32))


def with_dropout(strip: Strip, start_s: float, length_s: float) -> Strip:
    """Zeros from ``start_s`` for ``length_s`` seconds."""
    signal = strip.signal_uv.copy()
    first = round(start_s * strip.sampling_hz)
    signal[first : first + round(length_s * strip.sampling_hz)] = 0
    return strip.with_signal(signal)


def flat_line(strip: Strip, level_uv: int = 0) -> Strip:
    """A constant signal (all zeros by default)."""
    return strip.with_signal(np.full(strip.n_samples, level_uv, dtype=np.int32))


def with_clipping(strip: Strip) -> Strip:
    """Clip so that every R-peak has a plateau of at least 20 ms (and one sample more)."""
    fs = strip.sampling_hz
    need = round(0.02 * fs) + 1
    signal = strip.signal_uv.astype(np.int64)
    half = round(0.04 * fs)
    # an R-peak whose +-40 ms window leaves the strip has a cut window: it is not counted
    times = [t for t in strip.r_times_s if half <= round(t * fs) < strip.n_samples - half]
    tops = [int(signal[round(t * fs) - half : round(t * fs) + half + 1].max()) for t in times]
    for fraction in np.arange(0.9, 0.04, -0.05):
        level = int(fraction * min(tops))
        widths = []
        for time in times:
            centre = round(time * fs)
            widths.append(int(np.sum(signal[centre - half : centre + half + 1] >= level)))
        if min(widths) >= need:
            return strip.with_signal(np.minimum(signal, level).astype(np.int32))
    raise ValueError("no clipping level gives every R-peak a 20 ms plateau")
