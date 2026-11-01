"""Conditioning filters (ADR 0006 section 5; tests file, "Filter responses"). Synthetic signals."""

from functools import cache

import numpy as np
import pytest
import tolerances as tol

from ownvitals.ecg import filters, sqi
from ownvitals.ecg.pipeline import analyse

RATES = (300, 500)


def db(frequencies: list[float], fs: int) -> np.ndarray:
    return filters.response_db(fs, np.array(frequencies))


@pytest.mark.parametrize("fs", RATES)
def test_the_pass_band_is_flat_to_within_a_decibel(fs: int) -> None:
    frequencies = np.linspace(1.0, 30.0, 59)
    response = filters.response_db(fs, frequencies)
    assert response.max() <= tol.PASSBAND_DB[1]
    assert response.min() >= tol.PASSBAND_DB[0]
    assert abs(db([1.0], fs)[0] - (-0.5)) < 0.1  # about -0.5 dB at 1 Hz


def test_the_attenuation_at_30_hz_is_the_expected_one() -> None:
    assert db([30.0], 300)[0] == pytest.approx(-0.7, abs=0.1)
    assert db([30.0], 500)[0] == pytest.approx(-0.8, abs=0.1)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("corner", [0.5, 40.0])
def test_the_corners_are_6_db_down(fs: int, corner: float) -> None:
    assert db([corner], fs)[0] == pytest.approx(tol.CORNER_DB, abs=tol.CORNER_TOLERANCE_DB)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("mains", [50.0, 60.0])
def test_both_mains_frequencies_are_notched(fs: int, mains: float) -> None:
    narrow = db([mains - 0.1, mains, mains + 0.1], fs)
    wide = db([mains - 0.5, mains + 0.5], fs)
    assert narrow.max() <= tol.MAINS_NARROW_MAX_DB
    assert wide.max() <= tol.MAINS_WIDE_MAX_DB


@pytest.mark.parametrize("fs", RATES)
def test_the_response_at_49_5_hz_is_about_28_to_30_db_down(fs: int) -> None:
    assert -30.5 <= db([49.5], fs)[0] <= -27.5


@pytest.mark.parametrize("fs", RATES)
def test_slow_drift_is_removed(fs: int) -> None:
    assert db([0.05], fs)[0] <= tol.DRIFT_MAX_DB


@pytest.mark.parametrize("fs", RATES)
def test_a_symmetric_pulse_keeps_its_peak_zero_phase(fs: int) -> None:
    n = 20 * fs + 1  # odd, so the pulse sits on the middle sample
    t = (np.arange(n) - n // 2) / fs
    pulse = np.exp(-0.5 * (t / 0.02) ** 2)
    out = filters.condition(pulse, fs)
    assert abs(int(np.argmax(out)) - n // 2) <= tol.PULSE_PEAK_SHIFT_SAMPLES
    assert np.allclose(out, out[::-1], atol=1e-9)  # and it stays symmetric


@pytest.mark.parametrize("fs", RATES)
def test_sinusoids_through_the_real_function_match_the_design(fs: int) -> None:
    n = 30 * fs
    t = np.arange(n) / fs
    middle = slice(10 * fs, 20 * fs)
    for frequency in (0.5, 1.0, 5.0, 10.0, 20.0, 30.0, 40.0, 49.5, 50.5, 59.5, 60.5):
        x = np.sin(2.0 * np.pi * frequency * t)
        y = filters.condition(x, fs)
        gain_db = 20.0 * np.log10(np.std(y[middle]) / np.std(x[middle]))
        design_db = filters.response_db(fs, [frequency])[0]
        assert abs(gain_db - design_db) <= tol.REAL_FUNCTION_TOLERANCE_DB, frequency


@pytest.mark.parametrize("fs", RATES)
def test_the_median_is_subtracted_first(fs: int) -> None:
    rng = np.random.default_rng([5, fs])
    x = rng.standard_normal(10 * fs)
    assert np.allclose(filters.condition(x + 123.4, fs), filters.condition(x, fs), atol=1e-9)


def test_a_signal_of_one_sample_cannot_be_filtered() -> None:
    with pytest.raises(ValueError, match="at least two samples"):
        filters.condition([1.0], 300)


def test_the_design_is_second_order_sections_per_sampling_rate() -> None:
    for fs in RATES:
        sos = filters.design(fs)
        assert sos.shape == (1 + 2 + 2, 6)  # high-pass 1, two notches 1 each, low-pass 2
        assert not np.array_equal(sos, filters.design(600 if fs == 300 else 300))


def test_a_caller_cannot_change_the_cached_design() -> None:
    sos = filters.design(300)
    sos[:] = 0.0
    assert filters.design(300).any()


# --- the padding of the chain and the edge windows (issue #40, note from #46's round 4) -----------


@cache
def edge_kurtosis(fs: int, seeds: int) -> list[tuple[float, float, float, float]]:
    """(first window, last window, same without the settling margin) for white noise."""
    rows = []
    for seed in range(seeds):
        rng = np.random.default_rng([seed, fs, 91])
        noise = np.rint(rng.standard_normal(30 * fs) * 100.0).astype(np.int32)
        windows = analyse([noise], fs).windows  # type: ignore[union-attr]
        y = filters.condition(noise / 1000.0, fs)
        raw_first = sqi.kurtosis(y[: 10 * fs])
        raw_last = sqi.kurtosis(y[20 * fs :])
        rows.append((windows[0].ksqi, windows[-1].ksqi, raw_first, raw_last))
    return rows  # type: ignore[return-value]


@pytest.mark.parametrize("fs", RATES)
def test_white_noise_at_the_strips_edges_has_a_gaussian_kurtosis(fs: int) -> None:
    """The recorded kSQI of the first and last window is 3 +- 1, like the middle one; the odd
    padding turned an edge sample into a step that raised it to 14 (see the next test)."""
    low, high = tol.KSQI_WHITE_NOISE
    for first, last, _, _ in edge_kurtosis(fs, 60):
        assert low <= first <= high
        assert low <= last <= high


@pytest.mark.parametrize("fs", RATES)
def test_the_artefact_is_real_without_the_settling_margin(fs: int) -> None:
    """Why the margin exists: the same windows without it are far from Gaussian."""
    worst = max(max(raw_first, raw_last) for _, _, raw_first, raw_last in edge_kurtosis(fs, 60))
    assert worst > 4.0
