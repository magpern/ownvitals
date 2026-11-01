"""SQI bounds and components (tests file, "SQI bounds and components"; ADR 0006 section 7).

Synthetic signals only: generated strips and generated sinusoids.
"""

import ecgnoise
import numpy as np
import pytest
import tolerances as tol
from scenarios import RATES

from ecgsynth import make_strip
from ownvitals.ecg import sqi
from ownvitals.ecg.input import window_bounds
from ownvitals.ecg.pipeline import Analysed, QualityRejected, analyse


def windows_of(signal_uv: np.ndarray, fs: int) -> tuple[sqi.WindowIndices, ...]:
    """The per-window indices of a signal that the gate rejects (a sinusoid has no beats)."""
    result = analyse([signal_uv], fs)
    assert isinstance(result, QualityRejected)
    return result.windows


def sinusoid_uv(frequency: float, fs: int, offset_mv: float = 0.0) -> np.ndarray:
    t = np.arange(30 * fs) / fs
    return np.rint((np.sin(2.0 * np.pi * frequency * t) + offset_mv) * 1000.0).astype(np.int32)


# --- the bounds ----------------------------------------------------------------------------------


@pytest.mark.parametrize("fs", RATES)
def test_an_amplitude_of_12_mv_fails_as_out_of_range(fs: int) -> None:
    result = analyse([make_strip(1, 60.0, fs, amplitude_mv=12.0).signal_uv], fs)
    assert isinstance(result, QualityRejected)
    assert "amplitude_out_of_range" in result.reasons


@pytest.mark.parametrize("fs", RATES)
def test_an_amplitude_of_0_05_mv_fails_as_flat(fs: int) -> None:
    result = analyse([make_strip(1, 60.0, fs, amplitude_mv=0.05).signal_uv], fs)
    assert isinstance(result, QualityRejected)
    assert "flat_line" in result.reasons


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("rate", [25.0, 230.0])
def test_a_constant_rate_outside_30_to_220_bpm_is_implausible(fs: int, rate: float) -> None:
    result = analyse([make_strip(1, rate, fs, kind="constant").signal_uv], fs)
    assert isinstance(result, QualityRejected)
    assert "implausible_rate" in result.reasons


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("rate", [40.0, 60.0, 180.0])
def test_a_constant_rate_inside_the_range_passes_the_rate_row(fs: int, rate: float) -> None:
    result = analyse([make_strip(1, rate, fs, kind="constant").signal_uv], fs)
    assert not isinstance(result, QualityRejected)


# --- kSQI, pSQI and basSQI -----------------------------------------------------------------------


@pytest.mark.parametrize("fs", RATES)
def test_the_kurtosis_of_a_clean_strip_at_60_bpm_is_above_5(fs: int) -> None:
    result = analyse([make_strip(3, 60.0, fs).signal_uv], fs)
    for window in result.windows:  # type: ignore[union-attr]
        assert window.ksqi is not None
        assert window.ksqi > tol.KSQI_CLEAN_60BPM_MIN


@pytest.mark.parametrize("fs", RATES)
def test_white_noise_has_a_gaussian_kurtosis_in_the_middle_window(fs: int) -> None:
    low, high = tol.KSQI_WHITE_NOISE
    for seed in range(40):
        rng = np.random.default_rng([seed, fs, 17])
        noise = np.rint(rng.standard_normal(30 * fs) * 100.0).astype(np.int32)
        middle = windows_of(noise, fs)[2]
        assert (middle.start, middle.stop) == (10 * fs, 20 * fs)
        assert middle.ksqi is not None
        assert low <= middle.ksqi <= high


@pytest.mark.parametrize("fs", RATES)
def test_psqi_of_a_10_hz_sinusoid_is_one_and_of_a_30_hz_sinusoid_is_zero(fs: int) -> None:
    for window in windows_of(sinusoid_uv(10.0, fs), fs):
        assert window.psqi is not None
        assert window.psqi >= tol.PSQI_PASS_10HZ_MIN
    for window in windows_of(sinusoid_uv(30.0, fs), fs):
        assert window.psqi is not None
        assert window.psqi <= tol.PSQI_30HZ_MAX


@pytest.mark.parametrize("fs", RATES)
@pytest.mark.parametrize("offset_mv", [0.0, 1.0])
def test_bassqi_of_sinusoids_with_and_without_a_dc_offset(fs: int, offset_mv: float) -> None:
    """The window's mean is subtracted before Welch, so 1 mV of DC changes nothing."""
    for window in windows_of(sinusoid_uv(0.3, fs, offset_mv), fs):
        assert window.bassqi is not None
        assert window.bassqi <= tol.BASSQI_0_3HZ_MAX
    for window in windows_of(sinusoid_uv(10.0, fs, offset_mv), fs):
        assert window.bassqi is not None
        assert window.bassqi >= tol.BASSQI_10HZ_MIN


@pytest.mark.parametrize("fs", RATES)
def test_the_welch_estimate_equals_scipys(fs: int) -> None:
    from scipy.signal import welch

    x = np.random.default_rng([2, fs]).standard_normal(10 * fs) + 2.0
    freqs, density = sqi.welch_density(x, fs)
    expected_freqs, expected = welch(
        x - x.mean(),
        fs=fs,
        window="hann",
        nperseg=4 * fs,
        noverlap=2 * fs,
        detrend=False,  # type: ignore[arg-type]
    )
    assert np.allclose(freqs, expected_freqs)
    assert np.allclose(density, expected)


def test_a_constant_window_has_no_kurtosis_and_no_spectrum_ratio() -> None:
    assert sqi.kurtosis(np.zeros(100)) is None
    assert sqi.psqi(np.zeros(3000), 300) is None
    assert sqi.bassqi(np.zeros(3000), 300) is None


# --- windows, runs, agreement, templates --------------------------------------------------------


@pytest.mark.parametrize("fs", RATES)
def test_a_30_s_strip_has_five_windows_5_s_apart(fs: int) -> None:
    assert window_bounds(30 * fs, fs) == [(a * fs, (a + 10) * fs) for a in (0, 5, 10, 15, 20)]


@pytest.mark.parametrize("fs", RATES)
def test_the_last_window_is_aligned_to_the_end_of_the_strip(fs: int) -> None:
    bounds = window_bounds(27 * fs, fs)
    assert bounds[-1] == (17 * fs, 27 * fs)
    assert [a for a, _ in bounds] == [0, 5 * fs, 10 * fs, 15 * fs, 17 * fs]


@pytest.mark.parametrize("fs", RATES)
def test_a_strip_shorter_than_a_window_has_none_and_is_too_short(fs: int) -> None:
    assert window_bounds(10 * fs - 1, fs) == []
    result = analyse([make_strip(1, 60.0, fs).signal_uv[: 9 * fs]], fs)
    assert result == QualityRejected(reasons=("too_short",), windows=())


def test_runs_of_passing_windows() -> None:
    assert sqi._runs([True, True, False, True, False, True, True, True]) == [(0, 1), (3, 3), (5, 7)]
    assert sqi._runs([False, False]) == []
    assert sqi._runs([True]) == [(0, 0)]


def test_agreement_is_matched_over_the_union() -> None:
    a = np.array([100.0, 400.0, 700.0])
    assert sqi.agreement(a, a, 1.0) == 1.0
    assert sqi.agreement(a, a + 0.9, 1.0) == 1.0  # within the tolerance, inclusive
    assert sqi.agreement(a, a + 1.0, 1.0) == 1.0
    assert sqi.agreement(a, a + 1.1, 1.0) == 0.0
    assert sqi.agreement(a, a[:2], 1.0) == pytest.approx(2 / 3)
    assert sqi.agreement(a, np.append(a, 900.0), 1.0) == pytest.approx(3 / 4)
    assert sqi.agreement(np.zeros(0), np.zeros(0), 1.0) == 0.0


def test_a_beat_is_matched_once() -> None:
    assert sqi.agreement(np.array([100.0, 101.0]), np.array([100.5]), 1.0) == pytest.approx(1 / 2)


def test_the_longest_gap_counts_the_window_edges() -> None:
    beats = np.array([600.0, 900.0])
    assert sqi.longest_gap_s(beats, 0, 3000, 300) == pytest.approx((3000 - 900) / 300)
    assert sqi.longest_gap_s(np.zeros(0), 0, 3000, 300) == pytest.approx(10.0)


def test_the_rate_needs_two_beats() -> None:
    assert sqi.rate_bpm(np.array([300.0]), 300) is None
    assert sqi.rate_bpm(np.array([0.0, 300.0, 600.0]), 300) == pytest.approx(60.0)


def test_clipping_is_a_run_of_20_ms_at_the_raw_extremes_only() -> None:
    fs = 300
    raw = np.zeros(1000, dtype=np.int64)
    raw[100:106] = 500  # 6 samples = 20 ms at the maximum
    raw[200:205] = -500  # 5 samples: too short
    raw[300:320] = 100  # long, but not at an extreme
    mask = sqi.clipped_samples(raw, fs)
    assert mask[100:106].all()
    assert not mask[200:205].any()
    assert not mask[300:320].any()


def test_reasons_come_in_the_order_of_the_adr() -> None:
    reasons = sqi.window_reasons(
        bsqi=0.5, rate=10.0, gap_s=5.0, tsqi=0.1, amplitude_mv=0.0, clipped=True
    )
    assert reasons == tuple(
        r for r in sqi.REASONS if r != "too_short" and r != "amplitude_out_of_range"
    )
    assert (
        sqi.window_reasons(
            bsqi=1.0, rate=60.0, gap_s=1.0, tsqi=0.9, amplitude_mv=1.0, clipped=False
        )
        == ()
    )


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"bsqi": 0.8999}, "detector_disagreement"),
        ({"rate": 29.9}, "implausible_rate"),
        ({"rate": 220.1}, "implausible_rate"),
        ({"rate": None}, "implausible_rate"),
        ({"gap_s": 3.01}, "beat_gap"),
        ({"tsqi": 0.659}, "low_template_correlation"),
        ({"tsqi": None}, "low_template_correlation"),
        ({"amplitude_mv": 0.0999}, "flat_line"),
        ({"amplitude_mv": 10.01}, "amplitude_out_of_range"),
        ({"clipped": True}, "clipping"),
    ],
)
def test_each_row_of_the_table_fails_just_beyond_its_bound(
    kwargs: dict[str, object], reason: str
) -> None:
    base = {
        "bsqi": 0.9,
        "rate": 30.0,
        "gap_s": 3.0,
        "tsqi": 0.66,
        "amplitude_mv": 0.1,
        "clipped": False,
    }
    assert sqi.window_reasons(**base) == ()  # the bounds are inclusive
    assert sqi.window_reasons(**{**base, **kwargs}) == (reason,)  # type: ignore[arg-type]
    base_high = {**base, "rate": 220.0, "amplitude_mv": 10.0}
    assert sqi.window_reasons(**base_high) == ()


def test_the_template_correlation_of_identical_beats_is_one() -> None:
    fs = 300
    y = np.zeros(3000)
    beats = np.arange(150, 3000, 300).astype(float)
    for b in beats:
        y[int(b) - 5 : int(b) + 6] = np.hanning(11)
    assert sqi.template_correlation(y, beats[:-1], 0, 3000) == pytest.approx(1.0)
    assert sqi.template_correlation(y, beats[:1], 0, 3000) is None
    assert (
        sqi.template_correlation(y, np.array([30.0, 330.0]), 0, 3000) is None
    )  # leaves the window
    assert fs == 300


# --- the per-beat correlation, the distorted beats and the trimmed edges (amended 2026-10-31) ----


def spike_train(
    beats: np.ndarray, n: int, shape_at: dict[int, np.ndarray] | None = None
) -> np.ndarray:
    """A signal of identical 21-sample pulses at the given sample positions (some replaced)."""
    y = np.zeros(n)
    for i, b in enumerate(beats.astype(int)):
        pulse = (shape_at or {}).get(i, np.hanning(23)[1:-1])
        y[b - 10 : b + 11] += pulse
    return y


def test_a_fixed_segment_correlation_ignores_the_neighbours_of_an_irregular_rhythm() -> None:
    """Segments of half the median RR would reach the neighbouring pulse of a short interval;
    the fixed QRS segment holds one pulse, so identical beats correlate perfectly at any rhythm."""
    beats = np.array([300, 700, 1100, 1250, 1650, 2100, 2500, 2900], dtype=float)
    y = spike_train(beats, 3300)
    fixed = sqi.beat_correlations(y, beats, 0, 3300, half=30)
    assert np.allclose(fixed, 1.0)
    window = sqi.beat_correlations(y, beats, 0, 3300)  # half the median RR (200 samples)
    assert window.min() < 0.999  # the 150-sample interval puts a neighbour into the segment


def test_a_distorted_beat_has_a_low_correlation_and_the_others_do_not() -> None:
    beats = np.arange(300, 3000, 300).astype(float)
    wrong = np.hanning(23)[1:-1] * np.linspace(1.0, -1.0, 21)  # a different shape
    y = spike_train(beats, 3300, {4: wrong})
    r = sqi.beat_correlations(y, beats, 0, 3300, half=30)
    assert r[4] < 0.5
    assert np.delete(r, 4) == pytest.approx(np.ones(r.shape[0] - 1))


def test_a_beat_whose_segment_leaves_the_window_is_not_assessed() -> None:
    beats = np.array([20.0, 320.0, 620.0, 920.0, 1180.0])
    y = spike_train(beats[1:], 1200)
    r = sqi.beat_correlations(y, beats, 0, 1200, half=30)
    assert np.isnan(r[0])
    assert not np.isnan(r[1:4]).any()
    assert np.isnan(sqi.beat_correlations(y, beats[:1], 0, 1200, half=30)).all()  # one beat: none
    assert np.isnan(sqi.beat_correlations(y, beats, 0, 400, half=30)).sum() >= 3


@pytest.mark.parametrize(
    ("distorted", "expected"),
    [
        ((), (0, 29)),  # nothing is trimmed
        ((2,), (3, 29)),  # a distorted beat 2 s into the span takes the beats before it out
        ((4,), (5, 29)),
        ((5,), (0, 29)),  # 5 s from the first beat is not "within" 5 s: an interior beat
        ((15,), (0, 29)),
        ((27,), (0, 26)),  # near the end it takes the beats after it out
        ((24,), (0, 29)),
        ((2, 4, 28), (5, 27)),  # the last distorted one near each end counts
    ],
)
def test_the_edges_of_a_span_are_trimmed_up_to_a_distorted_beat_near_them(
    distorted: tuple[int, ...], expected: tuple[int, int]
) -> None:
    fs = 300
    beats = ((0.5 + np.arange(30)) * fs).astype(np.float64)  # one a second, 0.5 s .. 29.5 s
    r = np.ones(30)
    r[list(distorted)] = 0.5
    assert sqi.verified_range(beats, r, fs) == expected


def test_an_unassessed_beat_is_neither_distorted_nor_trimmed() -> None:
    fs = 300
    beats = ((0.5 + np.arange(30)) * fs).astype(np.float64)
    r = np.ones(30)
    r[0] = r[-1] = np.nan
    assert sqi.verified_range(beats, r, fs) == (0, 29)


def test_the_threshold_is_below_every_clean_beat_and_above_the_wrong_ones() -> None:
    """DISTORTED_R sits under the lowest correlation of a correctly timed beat of the clean
    and the noisy conditions and over the ones a burst displaces (see docs/dev/ecg.md)."""
    assert 0.5 < sqi.DISTORTED_R < 0.95


def two_burst_strip() -> np.ndarray:
    """A 70 bpm strip with a 3-s burst of movement at 0 dB at each end (synthetic)."""
    strip = make_strip(3, 70.0, 300)
    rng = np.random.default_rng(3)
    bursts = (
        ecgnoise.motion_burst(rng, strip, 0.0, 3.0, 0.0),
        ecgnoise.motion_burst(rng, strip, 27.0, 3.0, 0.0),
    )
    return ecgnoise.noisy(strip, *bursts).signal_uv


def test_a_burst_at_each_end_trims_the_span_to_the_beats_between_them() -> None:
    result = analyse([two_burst_strip()], 300)
    assert isinstance(result, Analysed)
    first, last = result.rpeaks[0] / 300, result.rpeaks[-1] / 300
    assert 3.0 < first < 5.0 + 3.0
    assert 20.0 < last < 27.0
    assert result.span_s == pytest.approx(last - first)  # measured on the beats left
    assert result.span_s >= sqi.MIN_SPAN_S


def test_a_span_under_20_s_after_the_trim_fails_with_too_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a threshold that makes ordinary beats distorted and a long trim zone, little is
    left: the recording fails as `too_short` although windows in the middle passed, and the
    reasons are the usual ones (`too_short` first, then those of the failing windows)."""
    monkeypatch.setattr(sqi, "DISTORTED_R", 0.995)
    monkeypatch.setattr(sqi, "EDGE_TRIM_S", 12.0)
    result = analyse([two_burst_strip()], 300)
    assert isinstance(result, QualityRejected)
    assert result.reasons[0] == "too_short"
    assert any(w.passed for w in result.windows)
    assert set(result.reasons[1:]) == {r for w in result.windows for r in w.reasons}


# --- confirmed beats and the re-indexing of the bridging intervals after the trim ----------------


def test_confirmed_beats_drops_unconfirmed_beats_and_reports_the_bridged_interval() -> None:
    beats = np.array([100.0, 200.0, 300.0, 400.0, 500.0, 600.0])
    flags = np.array([True, True, False, True, True, True])
    kept, bridging = sqi.confirmed_beats(beats, flags)
    assert kept.tolist() == [100.0, 200.0, 400.0, 500.0, 600.0]
    assert bridging == (1,)  # kept beat 1 (200) to kept beat 2 (400)


def test_several_dropped_beats_in_a_row_bridge_one_interval() -> None:
    beats = np.arange(100.0, 800.0, 100.0)
    flags = np.array([True, False, False, True, True, False, True])
    kept, bridging = sqi.confirmed_beats(beats, flags)
    assert kept.tolist() == [100.0, 400.0, 500.0, 700.0]
    assert bridging == (0, 2)


def test_dropped_beats_at_the_ends_bridge_nothing() -> None:
    beats = np.arange(100.0, 700.0, 100.0)
    flags = np.array([False, False, True, True, False, False])
    kept, bridging = sqi.confirmed_beats(beats, flags)
    assert kept.tolist() == [300.0, 400.0]
    assert bridging == ()


def test_all_beats_confirmed_means_nothing_is_bridged() -> None:
    kept, bridging = sqi.confirmed_beats(np.array([1.0, 2.0, 3.0]), np.ones(3, dtype=bool))
    assert kept.tolist() == [1.0, 2.0, 3.0]
    assert bridging == ()


def test_the_trim_keeps_only_the_bridging_intervals_between_the_kept_beats_and_reindexes() -> None:
    bridging = (1, 4, 7, 10)  # intervals 1-2, 4-5, 7-8, 10-11
    # beats 3..9 are kept: intervals 3..8 lie between them
    assert sqi.trimmed_bridging(bridging, 3, 9) == (1, 4)
    assert sqi.trimmed_bridging(bridging, 0, 11) == (1, 4, 7, 10)
    assert sqi.trimmed_bridging(bridging, 5, 7) == ()  # the interval 7 would leave beat 7
    assert sqi.trimmed_bridging(bridging, 4, 8) == (0, 3)  # intervals 4 and 7, from beat 4


def test_the_re_indexed_bridging_intervals_point_at_the_same_intervals_after_the_trim() -> None:
    kept = np.array([0.0, 100.0, 300.0, 400.0, 600.0, 700.0])
    bridging = (1, 3)  # 100-300 and 400-600 span a dropped beat
    first, last = 1, 5
    rebased = sqi.trimmed_bridging(bridging, first, last)
    trimmed = kept[first : last + 1]
    for i, j in zip(rebased, bridging, strict=True):
        assert trimmed[i] == kept[j]
        assert trimmed[i + 1] == kept[j + 1]
