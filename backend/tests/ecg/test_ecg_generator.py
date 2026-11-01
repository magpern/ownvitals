"""The generator and the noise models (tests file, "Generator" and "Noise"). Synthetic only.

These tests pin down the properties of ``ecgsynth`` and ``ecgnoise`` that the other tests rely
on: the R-events are the ground truth, the amplitude and the SNR are what the specification
defines, and each rhythm and artefact is what its bullet says.
"""

from collections.abc import Callable

import ecgnoise
import numpy as np
import pytest
from numpy.typing import NDArray
from scenarios import RATES
from scipy.signal import welch

from ecgsynth import (
    DURATION_S,
    ECTOPIC_WIDTH,
    Strip,
    ecgsyn_rr_samples,
    f_waves,
    fourier_reader,
    make_strip,
    peak_to_peak_mv,
)

SEEDS = range(12)
NoiseModel = Callable[[np.random.Generator, Strip, float], NDArray[np.float64]]


def r_heights(strip: Strip) -> list[float]:
    half = round(0.04 * strip.sampling_hz)
    out = []
    for t, kind in zip(strip.r_times_s, strip.beat_types, strict=True):
        c = round(t * strip.sampling_hz)
        if kind == "N" and half <= c < strip.n_samples - half:
            out.append(float(strip.clean_mv[c - half : c + half + 1].max()))
    return out


# --- the strip --------------------------------------------------------------------------------


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("kind", ["sinus", "af", "ectopic", "respiratory", "constant"])
def test_a_strip_is_30_s_of_int32_microvolts_labelled_synthetic(fs: int, kind: str) -> None:
    strip = make_strip(1, 70.0, fs, kind=kind)  # type: ignore[arg-type]
    assert strip.synthetic is True
    assert strip.signal_uv.dtype == np.int32
    assert strip.n_samples == round(DURATION_S * fs) == 30 * fs
    assert strip.sampling_hz == fs
    assert np.array_equal(strip.signal_uv, np.rint(strip.clean_mv * 1000.0).astype(np.int32))
    assert len(strip.beat_types) == strip.r_times_s.shape[0]
    assert strip.r_times_s[0] >= 0.0
    assert strip.r_times_s[-1] < DURATION_S


def test_a_strip_is_deterministic_in_its_arguments() -> None:
    a = make_strip(5, 80.0, 300)
    b = make_strip(5, 80.0, 300)
    assert np.array_equal(a.signal_uv, b.signal_uv)
    assert np.array_equal(a.r_times_s, b.r_times_s)
    assert not np.array_equal(a.signal_uv, make_strip(6, 80.0, 300).signal_uv)
    assert not np.array_equal(a.r_times_s, make_strip(5, 81.0, 300).r_times_s)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("hr", [40.0, 75.0, 180.0])
@pytest.mark.parametrize("amplitude", [0.3, 1.0, 2.5])
def test_the_median_r_amplitude_is_a(fs: int, hr: float, amplitude: float) -> None:
    strip = make_strip(2, hr, fs, amplitude_mv=amplitude)
    assert np.median(r_heights(strip)) == pytest.approx(amplitude, rel=1e-6)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("hr", [40.0, 60.0, 100.0, 140.0, 180.0])
def test_the_r_events_are_the_maxima_of_the_signal(fs: int, hr: float) -> None:
    """The ground truth: the R wave peaks at the R-event, to within one sample."""
    strip = make_strip(3, hr, fs)
    half = round(0.06 * fs)
    checked = 0
    for t in strip.r_times_s:
        c = round(t * fs)
        if half <= c < strip.n_samples - half:
            peak = int(np.argmax(strip.clean_mv[c - half : c + half + 1])) - half
            assert abs(peak) <= 1
            checked += 1
    assert checked >= 20


def test_inverted_polarity_flips_the_signal_and_keeps_the_events() -> None:
    up = make_strip(4, 70.0, 300)
    down = make_strip(4, 70.0, 300, polarity=-1)
    assert np.array_equal(up.r_times_s, down.r_times_s)
    assert np.allclose(up.clean_mv, -down.clean_mv)


# --- the RR series ----------------------------------------------------------------------------


@pytest.mark.parametrize("hr", [40.0, 100.0, 180.0])
def test_the_default_rr_series_has_mean_60_over_hr_and_sd_2_percent(hr: float) -> None:
    for seed in SEEDS:
        samples = ecgsyn_rr_samples(np.random.default_rng(seed), hr)
        assert samples.shape == (256,)
        assert samples.mean() == pytest.approx(60.0 / hr, rel=1e-9)
        assert samples.std() == pytest.approx(0.02 * 60.0 / hr, rel=1e-9)


def test_the_series_is_read_between_its_samples_by_fourier_interpolation() -> None:
    samples = ecgsyn_rr_samples(np.random.default_rng(1), 60.0)
    read = fourier_reader(samples)
    assert all(read(float(k)) == pytest.approx(samples[k], abs=1e-12) for k in range(0, 256, 17))
    # A 0.25 Hz component keeps its amplitude between the samples (linear interpolation of
    # samples 1 s apart would damp it by about 19 %).
    tone = 0.02 * np.sin(2.0 * np.pi * 0.25 * np.arange(256))
    between = fourier_reader(tone)
    measured = np.array([between(x) for x in np.linspace(10.0, 20.0, 1001)])
    assert measured.max() == pytest.approx(0.02, rel=0.01)
    assert measured.min() == pytest.approx(-0.02, rel=0.01)


@pytest.mark.parametrize("fs", RATES)
def test_the_intervals_of_a_sinus_strip_follow_the_series(fs: int) -> None:
    for seed in SEEDS:
        strip = make_strip(seed, 72.0, fs)
        rr = np.diff(strip.r_times_s)
        assert rr.mean() == pytest.approx(60.0 / 72.0, rel=0.04)
        assert 0.005 < rr.std() / rr.mean() < 0.06


@pytest.mark.parametrize("fs", RATES)
def test_a_constant_series_has_constant_intervals(fs: int) -> None:
    strip = make_strip(1, 60.0, fs, kind="constant")
    assert np.allclose(np.diff(strip.r_times_s), 1.0, atol=1e-9)


# --- the rhythms ------------------------------------------------------------------------------


@pytest.mark.parametrize("fs", RATES)
def test_af_like_strips_have_lognormal_intervals_with_cv_025_and_sinus_labels(fs: int) -> None:
    cvs = []
    for seed in range(40):
        strip = make_strip(seed, 100.0 + 50.0 * seed / 39.0, fs, kind="af")
        rr = np.diff(strip.r_times_s)
        assert set(strip.beat_types) == {"N"}
        assert rr.min() >= 0.15
        cvs.append(rr.std(ddof=1) / rr.mean())
    assert np.mean(cvs) == pytest.approx(0.25, abs=0.04)


def test_af_like_strips_have_no_p_wave() -> None:
    """The P wave sits 150-250 ms before the R-event: flat in the AF-like strip once the f-waves
    (0.05 mV) are taken away, present in the sinus strip."""
    fs = 500
    sinus = make_strip(3, 60.0, fs, kind="constant")
    af = make_strip(3, 60.0, fs, kind="af")
    p_sinus = []
    p_af = []
    for strip, out in ((sinus, p_sinus), (af, p_af)):
        rr = np.diff(strip.r_times_s)
        for t, interval in zip(strip.r_times_s[1:-1], rr[1:], strict=False):
            if interval > 0.9:
                c = round(t * fs)
                out.append(float(strip.clean_mv[c - round(0.2 * fs) : c - round(0.13 * fs)].max()))
    assert np.median(p_sinus) > 0.1
    assert np.median(p_af) < 0.1  # f-waves of 0.05 mV only


def test_f_waves_are_0_05_mv_between_5_and_7_hz() -> None:
    wave = f_waves(np.random.default_rng(3), 30 * 500, 500)
    assert np.abs(wave).max() == pytest.approx(0.05, rel=1e-6)
    freqs, power = welch(wave, fs=500, nperseg=4096)
    assert power[(freqs >= 4.5) & (freqs <= 7.5)].sum() / power.sum() > 0.95


@pytest.mark.parametrize("fs", RATES)
def test_ectopic_beats_are_premature_with_a_full_compensatory_pause(fs: int) -> None:
    pairs = 0
    for seed in range(30):
        strip = make_strip(seed, 70.0, fs, kind="ectopic", n_ectopic=1 + seed % 2)
        times, types = strip.r_times_s, strip.beat_types
        for i, kind in enumerate(types):
            if kind != "V" or i < 2 or i + 1 >= len(types):
                continue
            preceding = times[i - 1] - times[i - 2]
            premature, pause = times[i] - times[i - 1], times[i + 1] - times[i]
            assert 0.6 - 1e-9 <= premature / preceding <= 0.7 + 1e-9
            assert premature + pause == pytest.approx(2.0 * preceding, abs=1e-9)
            pairs += 1
    assert pairs >= 30


@pytest.mark.parametrize("fs", RATES)
def test_ectopic_beats_have_a_wider_qrs_and_an_inverted_t_wave(fs: int) -> None:
    strip = make_strip(7, 60.0, fs, kind="ectopic", n_ectopic=2)
    times, types = strip.r_times_s, strip.beat_types

    def width_ms(i: int) -> float:
        """The width of the R wave at half its height, in ms."""
        c, h = round(times[i] * fs), round(0.1 * fs)
        segment = strip.clean_mv[c - h : c + h + 1]
        above = segment >= 0.5 * segment.max()
        lo = hi = h
        while lo > 0 and above[lo - 1]:
            lo -= 1
        while hi < 2 * h and above[hi + 1]:
            hi += 1
        return 1000.0 * (hi - lo + 1) / fs

    def t_mean(i: int) -> float:
        c = round(times[i] * fs)
        return float(strip.clean_mv[c + round(0.15 * fs) : c + round(0.3 * fs)].mean())

    inside = range(3, len(types) - 3)
    sinus = [i for i in inside if types[i] == "N" and "V" not in types[i - 2 : i + 3]]
    ectopic = [i for i in inside if types[i] == "V"]
    assert ectopic
    ratio = np.median([width_ms(i) for i in ectopic]) / np.median([width_ms(i) for i in sinus])
    # The Gaussians of Q, R and S are twice as wide; their overlap makes the half-height width of
    # the R wave grow somewhat less than twice.
    assert 0.8 * ECTOPIC_WIDTH <= ratio <= 1.15 * ECTOPIC_WIDTH
    assert np.median([t_mean(i) for i in ectopic]) < 0 < np.median([t_mean(i) for i in sinus])


@pytest.mark.parametrize("fs", RATES)
def test_respiratory_modulation_is_12_percent_of_the_intervals_at_a_quarter_hertz(fs: int) -> None:
    strip = make_strip(2, 60.0, fs, kind="respiratory")
    times = strip.r_times_s[:-1]
    rr = np.diff(strip.r_times_s)
    design = np.column_stack(
        [np.sin(2 * np.pi * 0.25 * times), np.cos(2 * np.pi * 0.25 * times), np.ones(rr.shape[0])]
    )
    coefficients, *_ = np.linalg.lstsq(design, rr, rcond=None)
    assert np.hypot(coefficients[0], coefficients[1]) / coefficients[2] == pytest.approx(
        0.12, abs=0.015
    )


# --- the noise models -------------------------------------------------------------------------


def snr_db(strip: Strip, noise: NDArray[np.float64]) -> float:
    return float(10.0 * np.log10((peak_to_peak_mv(strip) ** 2 / 8.0) / np.mean(noise**2)))


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("snr", [6.0, 12.0])
@pytest.mark.parametrize("model", [ecgnoise.white, ecgnoise.emg_like])
def test_white_and_emg_like_noise_have_the_specified_snr(
    fs: int, snr: float, model: NoiseModel
) -> None:
    strip = make_strip(1, 75.0, fs)
    noise = model(np.random.default_rng(1), strip, snr)
    assert snr_db(strip, noise) == pytest.approx(snr, abs=0.3)


@pytest.mark.parametrize("fs", RATES)
def test_emg_like_noise_has_no_energy_below_20_hz(fs: int) -> None:
    noise = ecgnoise.emg_like(np.random.default_rng(2), make_strip(1, 75.0, fs), 6.0)
    freqs, power = welch(noise, fs=fs, nperseg=2048)
    assert power[freqs < 10.0].sum() / power.sum() < 0.005


@pytest.mark.parametrize("fs", RATES)
def test_baseline_wander_is_two_sinusoids_of_amplitude_a(fs: int) -> None:
    strip = make_strip(1, 75.0, fs, amplitude_mv=0.8)
    noise = ecgnoise.baseline_wander(np.random.default_rng(1), strip)
    t = np.arange(noise.shape[0]) / fs
    design = np.column_stack(
        [f(2.0 * np.pi * hz * t) for hz in (0.3, 0.05) for f in (np.sin, np.cos)]
    )
    coefficients, residual, *_ = np.linalg.lstsq(design, noise, rcond=None)
    assert np.hypot(coefficients[0], coefficients[1]) == pytest.approx(0.8, rel=1e-6)
    assert np.hypot(coefficients[2], coefficients[3]) == pytest.approx(0.8, rel=1e-6)
    assert residual.max() < 1e-9


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("nominal", [50.0, 60.0])
def test_mains_is_within_0_2_hz_of_nominal_at_half_the_amplitude(fs: int, nominal: float) -> None:
    strip = make_strip(1, 75.0, fs)  # A = 1 mV
    noise = ecgnoise.mains(np.random.default_rng(4), strip, nominal)
    n_fft = 16 * noise.shape[0]
    spectrum = np.abs(np.fft.rfft(noise * np.hanning(noise.shape[0]), n=n_fft))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / fs)
    near = (freqs > nominal - 1.0) & (freqs < nominal + 1.0)
    assert abs(freqs[near][np.argmax(spectrum[near])] - nominal) <= 0.2 + 0.02
    third = 3.0 * nominal < fs / 2.0  # a third harmonic of 0.1 A below Nyquist
    expected_rms = np.sqrt((0.5**2 + (0.1**2 if third else 0.0)) / 2.0)
    assert np.sqrt(np.mean(noise**2)) == pytest.approx(expected_rms, rel=0.02)


@pytest.mark.parametrize("fs", RATES)
def test_a_motion_burst_is_band_limited_tapered_and_has_the_specified_snr(fs: int) -> None:
    strip = make_strip(1, 75.0, fs)
    noise = ecgnoise.motion_burst(np.random.default_rng(5), strip, 10.0, 2.0, 0.0)
    first, last = round(10.0 * fs), round(12.0 * fs)
    assert np.all(noise[:first] == 0.0)
    assert np.all(noise[last:] == 0.0)
    assert abs(noise[first]) < 0.05 * np.abs(noise).max()
    assert abs(noise[last - 1]) < 0.05 * np.abs(noise).max()
    burst = noise[first:last]
    assert snr_db(strip, burst) == pytest.approx(0.0, abs=1e-6)
    freqs, power = welch(burst, fs=fs, nperseg=len(burst) // 2)
    assert power[freqs > 15.0].sum() / power.sum() < 0.05


@pytest.mark.parametrize("fs", RATES)
def test_motion_bursts_cover_at_least_half_of_the_strip(fs: int) -> None:
    strip = make_strip(1, 75.0, fs)
    noise = ecgnoise.motion_bursts(np.random.default_rng(6), strip, 0.0, coverage=0.5)
    assert np.count_nonzero(noise) / strip.n_samples >= 0.45  # the tapers and zero crossings


@pytest.mark.parametrize("fs", RATES)
def test_an_electrode_pop_is_a_2a_step_decaying_with_tau_0_3_s(fs: int) -> None:
    strip = make_strip(1, 75.0, fs, amplitude_mv=0.9)
    pop = ecgnoise.electrode_pop(strip, 10.0)
    at = round(10.0 * fs)
    assert np.all(pop[:at] == 0.0)
    assert pop[at] == pytest.approx(2.0 * 0.9)
    assert pop[at + round(0.3 * fs)] == pytest.approx(2.0 * 0.9 / np.e, rel=1e-3)


@pytest.mark.parametrize("fs", RATES)
def test_dropout_flat_line_and_clipping_change_the_recorded_samples(fs: int) -> None:
    strip = make_strip(1, 75.0, fs)
    dropped = ecgnoise.with_dropout(strip, 9.0, 12.0)
    assert np.all(dropped.signal_uv[round(9.0 * fs) : round(21.0 * fs)] == 0)
    assert np.array_equal(dropped.signal_uv[: round(9.0 * fs)], strip.signal_uv[: round(9.0 * fs)])
    assert np.all(ecgnoise.flat_line(strip).signal_uv == 0)
    assert np.all(ecgnoise.flat_line(strip, 300).signal_uv == 300)
    clipped = ecgnoise.with_clipping(strip)
    top = int(clipped.signal_uv.max())
    plateau = round(0.02 * fs) + 1
    half = round(0.04 * fs)
    for t in strip.r_times_s:
        c = round(t * fs)
        assert np.sum(clipped.signal_uv[max(c - half, 0) : c + half + 1] == top) >= plateau
