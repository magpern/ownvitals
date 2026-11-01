"""Synthetic ECG generator for the ECG tests (ADR 0006 section 13, the test specification).

SYNTHETIC: every sample, time and parameter produced here is invented. There is no real or
public ECG recording in the repository (AGENTS.md); the output of ``make_strip`` is labelled
``synthetic=True``.

The model is the dynamical model of McSharry et al. (2003) with the paper's PQRST parameters
(Table I) and ECGSYN's heart-rate scaling. The RR series is explicit: interval ``k`` is the
series' value at the beat that starts it, and the angular velocity is ``2 pi / RR_k`` within
that interval, so the R-events fall exactly at the series' beat times and the phase ``theta``
is a known function of time. The amplitudes of the events are scaled by ``omega_k / omega_mean``,
so that the deflections of a beat do not depend on its RR (amended 2026-10-31, #40: without it
the integral of an event, which scales with ``1 / omega``, shrank the QRS after a short RR).
With ``x`` and ``y`` on their limit cycle, only the third equation of the model remains,
``dz/dt = f(theta(t)) - z``, a first-order filter that is solved exactly.

The default RR series is ECGSYN's: a Gaussian LF (0.1 Hz) and HF (0.25 Hz) spectrum, widths
0.01 Hz, LF/HF 0.5, random phases, 256 samples at 1 Hz, scaled to mean ``60 / HR`` s and SD 2 %
of it, and read between its samples by band-limited (Fourier-series) interpolation, never
linearly. Output: int32 microvolts at 300 or 500 Hz, 30-s strips, median R amplitude ``A``.

Kinds of rhythm: sinus, ``af`` (no P wave, f-waves, independent lognormal RR), ``ectopic``
(premature beats with a full compensatory pause, double-width QRS, inverted T) and
``respiratory`` (a sinusoidal RR series alone) and ``constant`` (a fixed RR, for the rate bounds
of the quality gate). Noise lives in ``tests/ecg/ecgnoise.py``.
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from scipy.signal import lfilter

SYNTHETIC = True

# McSharry et al. 2003, Table I: P, Q, R, S, T
THETA_I = np.array([-np.pi / 3, -np.pi / 12, 0.0, np.pi / 12, np.pi / 2])
A_I = np.array([1.2, -5.0, 30.0, -7.5, 0.75])
B_I = np.array([0.25, 0.1, 0.1, 0.1, 0.4])

DURATION_S = 30.0
WARM_S = 8.0  # model time before the strip, so the leak of z has settled
RR_SERIES_LEN = 256  # samples at 1 Hz (ECGSYN)
RR_SD_FRACTION = 0.02
LF_HZ, HF_HZ, BAND_HZ, LF_HF = 0.1, 0.25, 0.01, 0.5
ECTOPIC_WIDTH = 2.0  # QRS width factor of an ectopic beat
MIN_RR_S = 0.15  # an AF-like interval is never shorter (numerical safety)
MIN_PREMATURE_S = 0.25  # a premature interval is at least this long (amended 2026-10-31, #40)

Kind = Literal["sinus", "af", "ectopic", "respiratory", "constant"]
RrReader = Callable[[float], float]


@dataclass(frozen=True)
class Strip:
    """A generated strip and its ground truth."""

    signal_uv: NDArray[np.int32]
    sampling_hz: int
    r_times_s: NDArray[np.float64]  # R-events inside the strip
    beat_types: tuple[str, ...]  # "N" sinus, "V" ectopic, aligned with r_times_s
    clean_mv: NDArray[np.float64]  # the signal before noise (before rounding), in mV
    amplitude_mv: float  # the median R amplitude
    hr_bpm: float  # the nominal mean heart rate
    kind: str
    seed: int
    synthetic: bool = True

    @property
    def n_samples(self) -> int:
        return int(self.signal_uv.shape[0])

    def with_signal(self, signal_uv: NDArray[np.int32]) -> "Strip":
        return replace(self, signal_uv=signal_uv)


def ecgsyn_rr_samples(rng: np.random.Generator, hr_bpm: float) -> NDArray[np.float64]:
    """ECGSYN's RR process: 256 samples at 1 Hz, mean ``60 / HR`` s, SD 2 % of the mean."""
    n = RR_SERIES_LEN
    freqs = np.arange(n // 2 + 1) / n  # Hz, rfft bins
    psd = LF_HF * np.exp(-0.5 * ((freqs - LF_HZ) / BAND_HZ) ** 2) + np.exp(
        -0.5 * ((freqs - HF_HZ) / BAND_HZ) ** 2
    )
    amplitude = np.sqrt(psd)
    amplitude[0] = amplitude[-1] = 0.0  # no DC, no Nyquist component
    phase = rng.uniform(0.0, 2.0 * np.pi, size=freqs.shape)
    series = np.fft.irfft(amplitude * np.exp(1j * phase), n=n)
    mean_rr = 60.0 / hr_bpm
    return mean_rr + series * (RR_SD_FRACTION * mean_rr / np.std(series))


def fourier_reader(samples: NDArray[np.float64]) -> RrReader:
    """``r(t)``: the band-limited interpolation of periodic samples spaced 1 s apart."""
    n = samples.shape[0]
    coefficients = np.fft.rfft(samples)
    k = np.arange(coefficients.shape[0])
    weights = np.where((k == 0) | (k == n // 2), 1.0, 2.0) / n

    def read(t: float) -> float:
        return float(np.sum(weights * (coefficients * np.exp(2j * np.pi * k * t / n)).real))

    return read


def sinus_background(rng: np.random.Generator, hr_bpm: float, kind: Kind) -> RrReader:
    """The sinus RR as a function of time (s), for the kinds that read a series."""
    mean_rr = 60.0 / hr_bpm
    if kind == "constant":
        return lambda t: mean_rr
    if kind == "respiratory":
        phase = rng.uniform(0.0, 2.0 * np.pi)
        return lambda t: mean_rr * (1.0 + 0.12 * np.sin(2.0 * np.pi * 0.25 * t + phase))
    if kind == "ectopic":
        p1, p2 = rng.uniform(0.0, 2.0 * np.pi, size=2)
        return lambda t: (
            mean_rr
            * (
                1.0
                + 0.02 * np.sin(2.0 * np.pi * 0.1 * t + p1)
                + 0.02 * np.sin(2.0 * np.pi * 0.25 * t + p2)
            )
        )
    reader = fourier_reader(ecgsyn_rr_samples(rng, hr_bpm))
    offset = rng.uniform(0.0, RR_SERIES_LEN)
    return lambda t: reader(t + offset)


def lognormal_rr(rng: np.random.Generator, hr_bpm: float, cv: float = 0.25) -> float:
    sigma = float(np.sqrt(np.log1p(cv**2)))
    mu = float(np.log(60.0 / hr_bpm)) - sigma**2 / 2.0
    return max(float(rng.lognormal(mu, sigma)), MIN_RR_S)


def ectopic_times(rng: np.random.Generator, n_ectopic: int) -> list[float]:
    """Where the ectopic beats fall: anywhere in the strip, at least 3 s apart."""
    while True:
        times = sorted(float(v) for v in rng.uniform(0.2, DURATION_S - 0.7, size=n_ectopic))
        if all(b - a >= 3.0 for a, b in itertools.pairwise(times)):
            return times


def beat_series(
    rng: np.random.Generator, hr_bpm: float, kind: Kind, n_ectopic: int
) -> tuple[NDArray[np.float64], list[str]]:
    """Beat times (s, from before the strip to after it) and beat types."""
    end = DURATION_S + 2.0
    t = -WARM_S - rng.uniform(0.0, 60.0 / hr_bpm)
    times = [t]
    types = ["N"]
    read = None if kind == "af" else sinus_background(rng, hr_bpm, kind)
    planned = ectopic_times(rng, n_ectopic) if kind == "ectopic" else []
    previous: float | None = None  # the last sinus-to-sinus interval, None after a pause
    while t < end:
        if read is None:
            t += lognormal_rr(rng, hr_bpm)
            times.append(t)
            types.append("N")
        elif planned and previous is not None and t + 0.65 * previous >= planned[0]:
            coupling = rng.uniform(0.6, 0.7)
            premature = max(coupling * previous, MIN_PREMATURE_S)  # amended 2026-10-31, #40
            times.extend([t + premature, t + 2.0 * previous])
            types.extend(["V", "N"])
            t += 2.0 * previous
            planned.pop(0)
            previous = None
        else:
            interval = read(t)
            t += interval
            times.append(t)
            types.append("N")
            previous = interval
    return np.array(times), types


def wave_forcing(
    t: NDArray[np.float64],
    beats: NDArray[np.float64],
    ectopic: NDArray[np.bool_],
    hr_bpm: float,
    af: bool,
) -> NDArray[np.float64]:
    """The Gaussian forcing of the third equation, ``sum -a_i dtheta_i exp(...)``."""
    k = np.clip(np.searchsorted(beats, t, side="right") - 1, 0, beats.shape[0] - 2)
    u = (t - beats[k]) / (beats[k + 1] - beats[k])
    theta = 2.0 * np.pi * u - 2.0 * np.pi * (u >= 0.5)  # [-pi, pi), zero at every R-event
    owner = np.minimum(k + (u >= 0.5), beats.shape[0] - 1)  # the beat whose waves these are
    wide = ectopic[owner]
    factor = np.sqrt(hr_bpm / 60.0)
    shift = np.array([np.sqrt(factor), factor, 1.0, factor, np.sqrt(factor)])
    forcing = np.zeros_like(t)
    # A beat's deflections do not depend on its RR: the integral of an event scales with 1/omega,
    # so each event amplitude is scaled by omega_k / omega_mean (amended 2026-10-31, #40).
    omega_ratio = (60.0 / hr_bpm) / (beats[k + 1] - beats[k])
    for i in range(5):
        a = np.full_like(t, A_I[i])
        b = np.full_like(t, B_I[i] * factor)
        if i in (1, 2, 3):  # Q, R, S: twice as wide at the same height (a * b^2 is kept)
            b = np.where(wide, b * ECTOPIC_WIDTH, b)
            a = np.where(wide, a / ECTOPIC_WIDTH**2, a)
        if i == 4:
            a = np.where(wide, -a, a)  # inverted T
        if i == 0 and af:
            a = np.zeros_like(t)
        delta = np.mod(theta - THETA_I[i] * shift[i] + np.pi, 2.0 * np.pi) - np.pi
        forcing += -a * delta * np.exp(-(delta**2) / (2.0 * b**2))
    return forcing * omega_ratio


def make_strip(
    seed: int,
    hr_bpm: float,
    sampling_hz: int,
    *,
    kind: Kind = "sinus",
    amplitude_mv: float = 1.0,
    polarity: int = 1,
    n_ectopic: int = 1,
) -> Strip:
    """One 30-s strip, deterministic in its arguments. ``seed`` is the whole random state."""
    rng = np.random.default_rng([seed, round(hr_bpm * 10), sampling_hz])
    beats, types = beat_series(rng, hr_bpm, kind, n_ectopic)
    ectopic = np.array([b == "V" for b in types])
    n_warm = round(WARM_S * sampling_hz)
    n_total = n_warm + round(DURATION_S * sampling_hz)
    t = (np.arange(n_total) - n_warm) / sampling_hz
    forcing = wave_forcing(t, beats, ectopic, hr_bpm, kind == "af")
    h = 1.0 / sampling_hz  # dz/dt = f - z: exact decay, trapezoid of f
    decay = float(np.exp(-h))
    z = lfilter([h / 2.0, h / 2.0 * decay], [1.0, -decay], forcing)[n_warm:]
    z -= np.median(z)
    inside = (beats >= 0.0) & (beats < DURATION_S)
    r_times = beats[inside]
    r_types = tuple(str(v) for v in np.array(types)[inside])
    scale = amplitude_mv / _median_r_height(z, r_times, r_types, sampling_hz)
    clean = polarity * z * scale
    if kind == "af":
        clean = clean + f_waves(rng, clean.shape[0], sampling_hz)
    signal = np.rint(clean * 1000.0).astype(np.int32)
    return Strip(
        signal_uv=signal,
        sampling_hz=sampling_hz,
        r_times_s=r_times,
        beat_types=r_types,
        clean_mv=clean,
        amplitude_mv=amplitude_mv,
        hr_bpm=hr_bpm,
        kind=kind,
        seed=seed,
    )


def _median_r_height(
    z: NDArray[np.float64], r_times: NDArray[np.float64], types: tuple[str, ...], fs: int
) -> float:
    half = round(0.04 * fs)
    heights = []
    for time, kind in zip(r_times, types, strict=True):
        centre = round(time * fs)
        if kind == "N" and half <= centre < z.shape[0] - half:
            heights.append(z[centre - half : centre + half + 1].max())
    return float(np.median(heights))


def f_waves(rng: np.random.Generator, n: int, fs: int) -> NDArray[np.float64]:
    """0.05 mV f-waves: a sinusoid whose frequency wanders between 5 and 7 Hz."""
    t = np.arange(n) / fs
    p1, p2 = rng.uniform(0.0, 2.0 * np.pi, size=2)
    rate = 0.3  # Hz of the frequency wander
    phase = (
        2.0 * np.pi * (6.0 * t - np.cos(2.0 * np.pi * rate * t + p2) / (2.0 * np.pi * rate)) + p1
    )
    return 0.05 * np.sin(phase)


def peak_to_peak_mv(strip: Strip) -> float:
    """The median QRS peak-to-peak amplitude of the clean signal (``A_pp`` of the SNR)."""
    half = round(0.06 * strip.sampling_hz)
    values = []
    for time, kind in zip(strip.r_times_s, strip.beat_types, strict=True):
        centre = round(time * strip.sampling_hz)
        if kind == "N" and half <= centre < strip.n_samples - half:
            segment = strip.clean_mv[centre - half : centre + half + 1]
            values.append(segment.max() - segment.min())
    return float(np.median(values))
