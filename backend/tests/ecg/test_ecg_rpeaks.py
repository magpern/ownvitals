"""R-peak detection (ADR 0006 section 6): the two detectors, the polarity rule and the fiducial
point. Synthetic strips only."""

import numpy as np
import pytest
from scenarios import RATES

from ecgsynth import make_strip
from ownvitals.ecg import filters, rpeaks


def conditioned(strip) -> np.ndarray:
    return filters.condition(strip.signal_uv / 1000.0, strip.sampling_hz)


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("hr", [40.0, 75.0, 120.0, 175.0])
def test_both_detectors_find_every_beat_of_a_clean_strip_independently(fs: int, hr: float) -> None:
    strip = make_strip(11, hr, fs)
    y = conditioned(strip)
    inside = strip.r_times_s[(strip.r_times_s > 0.5) & (strip.r_times_s < 29.5)]
    for detector in (rpeaks.pan_tompkins, rpeaks.elgendi):
        found = detector(y, fs) / fs
        assert all(np.min(np.abs(found - t)) < 0.05 for t in inside), detector.__name__
        assert len(found) <= len(strip.r_times_s) + 1


@pytest.mark.parametrize("fs", RATES)
def test_the_fiducial_point_is_the_same_for_both_detectors(fs: int) -> None:
    strip = make_strip(12, 80.0, fs)
    detection = rpeaks.detect(conditioned(strip), fs)
    assert np.array_equal(detection.primary, detection.secondary)


@pytest.mark.parametrize("fs", RATES)
def test_the_r_peak_is_within_two_ms_of_the_true_event(fs: int) -> None:
    strip = make_strip(13, 70.0, fs)
    found = rpeaks.detect(conditioned(strip), fs).primary / fs
    for t in strip.r_times_s[(strip.r_times_s > 0.3) & (strip.r_times_s < 29.7)]:
        assert np.min(np.abs(found - t)) < 0.002


@pytest.mark.parametrize("fs", RATES)
def test_an_inverted_lead_gives_the_same_beats_and_the_polarity_is_minus_one(fs: int) -> None:
    upright, inverted = make_strip(14, 90.0, fs), make_strip(14, 90.0, fs, polarity=-1)
    a = rpeaks.detect(conditioned(upright), fs)
    b = rpeaks.detect(conditioned(inverted), fs)
    assert (a.polarity, b.polarity) == (1, -1)
    assert a.primary.shape == b.primary.shape
    assert np.max(np.abs(a.primary - b.primary)) < 1.0  # within a sample


@pytest.mark.parametrize("fs", RATES)
def test_beats_whose_window_leaves_the_strip_are_dropped(fs: int) -> None:
    y = np.zeros(10 * fs)
    y[10] = 1.0  # 33 ms from the start: its +-75 ms window leaves the strip
    y[5 * fs] = 1.0
    refined = rpeaks.refine(y, np.array([10, 5 * fs]), 1, fs)
    assert refined.tolist() == [5.0 * fs]


def test_the_refinement_is_a_parabola_through_three_points() -> None:
    fs = 300
    x = np.arange(300) / fs
    for true_peak in (0.500, 0.5013, 0.5027, 0.5040):
        y = -((x - true_peak) ** 2) * 1e4
        refined = rpeaks.refine(y, np.array([150]), 1, fs)[0] / fs
        assert refined == pytest.approx(true_peak, abs=1e-6)


def test_refinement_removes_duplicates() -> None:
    y = np.zeros(3000)
    y[1500] = 1.0
    assert rpeaks.refine(y, np.array([1490, 1510]), 1, 300).shape == (1,)


def test_the_polarity_of_a_signal_without_beats_is_positive() -> None:
    assert rpeaks.polarity_of(np.zeros(3000), np.zeros(0, dtype=np.int64), 300) == 1


@pytest.mark.parametrize("detector", [rpeaks.pan_tompkins, rpeaks.elgendi])
def test_a_flat_signal_has_no_beats(detector) -> None:
    assert detector(np.zeros(9000), 300).size == 0


def test_the_detectors_are_deterministic() -> None:
    y = conditioned(make_strip(15, 100.0, 300))
    assert np.array_equal(rpeaks.pan_tompkins(y, 300), rpeaks.pan_tompkins(y, 300))
    assert np.array_equal(rpeaks.elgendi(y, 300), rpeaks.elgendi(y, 300))


@pytest.mark.parametrize("fs", RATES)
def test_a_t_wave_is_not_a_beat(fs: int) -> None:
    """Large, wide T waves (a_T x 4) next to unit QRS complexes are not detected."""
    strip = make_strip(16, 60.0, fs)
    y = conditioned(strip).copy()
    for t in strip.r_times_s:  # add a T-like bump 250 ms after each beat
        centre = round((t + 0.25) * fs)
        k = np.arange(len(y))
        y += 0.5 * np.exp(-0.5 * ((k - centre) / (0.04 * fs)) ** 2)
    found = rpeaks.pan_tompkins(y, fs) / fs
    assert len(found) <= len(strip.r_times_s)
    for f in found:
        assert np.min(np.abs(strip.r_times_s - f)) < 0.1
