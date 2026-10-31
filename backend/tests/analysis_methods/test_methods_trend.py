"""Theil-Sen slope and (modified) Mann-Kendall test. SYNTHETIC data only.

Reference values are worked out by hand from the formulas in the module docstring (Mann 1945,
Kendall 1975, Sen 1968, Hamed and Rao 1998, as in Gilbert 1987 chapter 16-17); where scipy
implements the same quantity (Kendall's tau test, the Theil-Sen slope) the result is compared
with it as well. The simulations are seeded and so deterministic; the rates they measure are
properties of the method and are recorded in ``docs/dev/analysis-methods.md``.
"""

import json
import math
from collections.abc import Callable
from functools import cache
from statistics import NormalDist
from typing import cast

import numpy as np
import pytest
from methods_builders import SYNTHETIC, ar1, generator, irregular_times
from scipy import stats

from ownvitals.analysis.methods import trend as trend_module
from ownvitals.analysis.methods.series import Series
from ownvitals.analysis.methods.trend import (
    ALGO_VERSION,
    MAX_POINTS,
    Correction,
    Direction,
    autocorrelation,
    hamed_rao_factor,
    mann_kendall_variance,
    mann_kendall_z,
    pairwise,
    slope_interval,
    trend,
    two_sided_p,
)


def line(n: int, values: list[float]) -> Series:
    assert len(values) == n
    return Series.from_arrays(np.arange(float(n)), values)


def test_the_fixtures_are_labelled_synthetic() -> None:
    assert SYNTHETIC is True


# --- Mann-Kendall by hand ----------------------------------------------------------------------


def test_a_perfect_ramp_of_five_by_hand() -> None:
    # S = 10 (all ten pairs increase); Var(S) = 5 * 4 * 15 / 18 = 16.667
    # Z = (10 - 1) / sqrt(16.667) = 2.2045 -> p = 2 * (1 - Phi(2.2045)) = 0.0275
    result = trend(line(5, [1, 2, 3, 4, 5]), correction=Correction.NONE)
    assert result.s == 10
    assert result.var_s == pytest.approx(50 / 3)
    assert result.z == pytest.approx(2.2045, abs=1e-4)
    assert result.p_value == pytest.approx(0.0275, abs=1e-4)
    assert result.tau == 1.0
    assert (result.slope, result.slope_low, result.slope_high) == (1.0, 1.0, 1.0)
    assert result.significant
    assert result.direction is Direction.INCREASING


def test_ties_by_hand() -> None:
    # y = 1 2 2 3 3 3: of the 15 pairs 4 are tied (2,2 and 3,3,3) and 11 increase -> S = 11.
    # Var = [6*5*17 - (2*1*9 + 3*2*11)] / 18 = (510 - 84) / 18 = 23.667
    # Z = (11 - 1) / sqrt(23.667) = 2.0556 -> p = 0.0398
    # The pairwise slopes sorted: 0 0 0 0 .25 1/3 1/3 .4 .5 .5 .5 .5 2/3 1 1 -> median .4
    result = trend(line(6, [1, 2, 2, 3, 3, 3]), correction=Correction.NONE)
    assert result.s == 11
    assert result.var_s == pytest.approx(426 / 18)
    assert result.z == pytest.approx(2.0556, abs=1e-4)
    assert result.p_value == pytest.approx(0.0398, abs=1e-4)
    assert result.slope == pytest.approx(0.4)


def test_slope_interval_and_intercept_by_hand() -> None:
    # y = 1 3 2 5 4 at t = 0..4. The ten slopes (j-i pairs): 2, .5, 4/3, .75, -1, 1, 1/3, 3, 1, -1
    # sorted: -1 -1 1/3 .5 .75 1 1 4/3 2 3 -> median (.75 + 1) / 2 = .875.
    # S = 8 up - 2 down = 6; Var = 5*4*15/18 = 16.667; Z = 5 / 4.0825 = 1.2247 -> p = 0.2207.
    # C = 1.96 * 4.0825 = 8.0016; ranks (10 - 8.0016)/2 = 0.999 -> 1 and (10 + 8.0016)/2 + 1 = 10
    # -> the interval is the smallest and the largest slope: [-1, 3].
    # Intercept = median(y - .875 t) = median(1, 2.125, .25, 2.375, .5) = 1.
    result = trend(line(5, [1, 3, 2, 5, 4]), correction=Correction.NONE)
    assert result.s == 6
    assert result.z == pytest.approx(1.2247, abs=1e-4)
    assert result.p_value == pytest.approx(0.2207, abs=1e-4)
    assert result.slope == 0.875
    assert (result.slope_low, result.slope_high) == (-1.0, 3.0)
    assert result.intercept == 1.0
    assert not result.significant
    assert result.direction is Direction.NONE


def test_a_decreasing_series_is_the_mirror_image() -> None:
    up = trend(line(8, [1, 4, 2, 6, 5, 8, 7, 9]), correction=Correction.NONE)
    down = trend(line(8, [-1, -4, -2, -6, -5, -8, -7, -9]), correction=Correction.NONE)
    assert down.s == -up.s
    assert down.z == pytest.approx(-up.z)
    assert down.p_value == pytest.approx(up.p_value)
    assert down.slope == pytest.approx(-up.slope)
    assert (down.slope_low, down.slope_high) == pytest.approx((-up.slope_high, -up.slope_low))


def test_the_continuity_correction_and_the_zero_cases() -> None:
    assert mann_kendall_z(0, 10.0) == 0.0
    assert mann_kendall_z(5, 0.0) == 0.0  # no variance: constant series
    assert mann_kendall_z(5, 4.0) == pytest.approx(2.0)  # (5 - 1) / 2
    assert mann_kendall_z(-5, 4.0) == pytest.approx(-2.0)  # (-5 + 1) / 2
    assert two_sided_p(0.0) == 1.0
    assert two_sided_p(1.959964) == pytest.approx(0.05, abs=1e-6)


def test_a_constant_series_has_no_trend() -> None:
    result = trend(line(10, [80.0] * 10))
    assert (result.s, result.var_s, result.z, result.p_value) == (0, 0.0, 0.0, 1.0)
    assert result.slope == 0.0
    assert not result.significant
    assert result.direction is Direction.NONE


# --- cross-checks with scipy --------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("rounded", [False, True])
def test_s_variance_and_the_uncorrected_test_match_scipy_kendall_tau(
    seed: int, rounded: bool
) -> None:
    rng = generator(seed)
    times = irregular_times(rng, 35, 60.0)
    values = rng.normal(0.0, 1.0, size=times.size) + 0.02 * times
    if rounded:  # many ties, as scales with a 0.1 resolution give
        values = np.round(values, 0)
    result = trend(Series.from_arrays(times, values), correction=Correction.NONE)
    tau, scipy_p = stats.kendalltau(times, values, method="asymptotic")
    uncorrected_p = two_sided_p(result.s / math.sqrt(result.var_s))
    assert uncorrected_p == pytest.approx(cast("float", scipy_p), rel=1e-9)
    # S itself, from tau-a = S / (n (n-1) / 2) when there are no ties
    if not rounded:
        assert result.s == round(cast("float", tau) * times.size * (times.size - 1) / 2)


@pytest.mark.parametrize("seed", range(6))
def test_the_slope_matches_scipy_theilslopes_on_irregular_times(seed: int) -> None:
    rng = generator(100 + seed)
    times = irregular_times(rng, 45, 90.0, with_gaps=True)
    values = 80.0 - 0.03 * times + rng.normal(0.0, 0.8, size=times.size)
    result = trend(Series.from_arrays(times, values))
    expected_slope, _, _, _ = stats.theilslopes(values, times)
    assert result.slope == pytest.approx(cast("float", expected_slope), rel=1e-12)
    assert result.slope_low <= result.slope <= result.slope_high


def test_the_pairwise_slopes_are_the_textbook_double_loop() -> None:
    rng = generator(4)
    times = irregular_times(rng, 25, 40.0)
    values = rng.normal(size=times.size)
    s, slopes = pairwise(times, values)
    expected = sorted(
        (values[j] - values[i]) / (times[j] - times[i])
        for i in range(times.size)
        for j in range(i + 1, times.size)
    )
    np.testing.assert_allclose(slopes, expected, rtol=1e-12)
    assert s == sum(
        int(np.sign(values[j] - values[i]))
        for i in range(times.size)
        for j in range(i + 1, times.size)
    )


# --- Hamed-Rao ---------------------------------------------------------------------------------


def hand_ranks_ramp() -> np.ndarray:
    return np.arange(1.0, 11.0)


def test_the_hamed_rao_factor_by_hand() -> None:
    # Ranks 1..10: the centred ranks are -4.5 .. 4.5 with sum of squares 82.5.
    # lag 1: sum of products 57.75 -> rho_1 = 0.7; lag 2: 34.0 -> .412; lag 3: 12.25 -> .1485.
    # Anderson's 95 % limits for n = 10: lag 1 upper (-1 + 1.96 sqrt(8)) / 9 = .505,
    # lag 2 .523: only lag 1 is significant.
    # n/n* = 1 + 2 / (10 * 9 * 8) * (9 * 8 * 7) * 0.7 = 1 + 2 * 504 * 0.7 / 720 = 1.98
    rho = autocorrelation(hand_ranks_ramp())
    assert rho[1] == pytest.approx(0.7)
    assert rho[2] == pytest.approx(34.0 / 82.5)
    assert rho[3] == pytest.approx(12.25 / 82.5)
    factor, lags = hamed_rao_factor(hand_ranks_ramp())
    assert lags == (1,)
    assert factor == pytest.approx(1.98)


def reference_factor(ranks: np.ndarray, alpha: float = 0.05) -> tuple[float, tuple[int, ...]]:
    """Hamed and Rao (1998), equation 9, with explicit sums."""
    n = len(ranks)
    mean = sum(ranks) / n
    denominator = sum((r - mean) ** 2 for r in ranks)
    z = NormalDist().inv_cdf(1 - alpha / 2)
    total = 0.0
    lags = []
    for k in range(1, n - 1):
        rho = sum((ranks[i] - mean) * (ranks[i + k] - mean) for i in range(n - k)) / denominator
        low = (-1 - z * math.sqrt(n - k - 1)) / (n - k)
        high = (-1 + z * math.sqrt(n - k - 1)) / (n - k)
        if rho < low or rho > high:
            lags.append(k)
            total += (n - k) * (n - k - 1) * (n - k - 2) * rho
    return 1 + 2 * total / (n * (n - 1) * (n - 2)), tuple(lags)


@pytest.mark.parametrize("seed", range(8))
def test_the_hamed_rao_factor_matches_an_explicit_reference(seed: int) -> None:
    rng = generator(seed)
    n = int(rng.integers(8, 70))
    ranks = stats.rankdata(ar1(rng, n, float(rng.uniform(-0.5, 0.8))))
    got = hamed_rao_factor(np.asarray(ranks))
    want = reference_factor(np.asarray(ranks))
    assert got[1] == want[1]
    assert got[0] == pytest.approx(want[0], rel=1e-12)


def test_negative_autocorrelation_is_not_used_to_deflate_the_variance() -> None:
    n = 40
    alternating = np.where(np.arange(n) % 2 == 0, 1.0, -1.0) + 0.05 * np.arange(n) % 0.7
    ranks = np.asarray(stats.rankdata(alternating))
    raw, lags = hamed_rao_factor(ranks)
    assert raw < 1.0
    assert 1 in lags
    result = trend(line(n, list(alternating)))
    assert result.n_over_n_star_raw < 1.0
    assert result.n_over_n_star == 1.0
    assert result.var_s_used == result.var_s


def test_plain_and_modified_agree_when_there_is_no_autocorrelation_to_correct() -> None:
    plain = trend(line(5, [1, 3, 2, 5, 4]), correction=Correction.NONE)
    modified = trend(line(5, [1, 3, 2, 5, 4]))
    assert plain.n_over_n_star == plain.n_over_n_star_raw == 1.0
    assert plain.significant_lags == ()
    assert modified.correction is Correction.HAMED_RAO
    assert modified.n_over_n_star >= 1.0
    assert modified.var_s_used == pytest.approx(modified.var_s * modified.n_over_n_star)


def test_the_correction_widens_the_test_and_the_interval_for_an_autocorrelated_series() -> None:
    rng = generator(31)
    values = ar1(rng, 100, 0.7) + 0.01 * np.arange(100)
    series = Series.from_arrays(np.arange(100.0), values)
    plain = trend(series, correction=Correction.NONE)
    modified = trend(series)
    assert modified.n_over_n_star > 1.5
    assert modified.p_value > plain.p_value
    assert modified.slope == plain.slope
    assert modified.slope_high - modified.slope_low > plain.slope_high - plain.slope_low


def test_sens_interval_picks_interpolated_ranks_by_hand() -> None:
    # Ten slopes 1..10 and C = 3: ranks (10 - 3) / 2 = 3.5 and (10 + 3) / 2 + 1 = 7.5.
    var_for_c3 = (3.0 / NormalDist().inv_cdf(0.975)) ** 2
    assert slope_interval(np.arange(1.0, 11.0), var_for_c3, 0.05) == pytest.approx((3.5, 7.5))
    # C = 0: both limits at ranks N/2 and N/2 + 1 -> the two middle slopes
    assert slope_interval(np.arange(1.0, 11.0), 0.0, 0.05) == pytest.approx((5.0, 6.0))
    # a huge C is clipped to the smallest and the largest slope
    assert slope_interval(np.arange(1.0, 11.0), 1e9, 0.05) == (1.0, 10.0)


# --- properties --------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_a_monotone_series_is_a_significant_trend(seed: int, sign: float) -> None:
    rng = generator(seed)
    times = irregular_times(rng, 30, 90.0, with_gaps=bool(seed % 2))
    values = sign * np.sort(rng.normal(80.0, 3.0, size=times.size))
    result = trend(Series.from_arrays(times, values))
    assert result.significant
    assert result.direction is (Direction.INCREASING if sign > 0 else Direction.DECREASING)
    assert result.p_value < 1e-3  # a curved trend leaves structured residuals: n/n* is large
    assert sign * result.slope > 0
    assert result.tau == sign


def test_a_noisy_trend_is_found_and_its_slope_is_inside_the_interval() -> None:
    rng = generator(40)
    times = irregular_times(rng, 80, 120.0)
    values = 82.0 - 0.05 * times + rng.normal(0.0, 0.7, size=times.size)
    result = trend(Series.from_arrays(times, values))
    assert result.direction is Direction.DECREASING
    assert result.slope_low <= -0.05 <= result.slope_high
    assert result.slope == pytest.approx(-0.05, abs=0.01)


def test_the_slope_and_the_test_do_not_depend_on_the_input_order() -> None:
    rng = generator(8)
    times = irregular_times(rng, 40, 60.0)
    values = rng.normal(size=times.size)
    order = rng.permutation(times.size)
    a = trend(Series.from_arrays(times, values))
    b = trend(Series.from_arrays(times[order], values[order]))
    assert a == b


def test_shifting_the_times_changes_neither_the_test_nor_the_slope() -> None:
    rng = generator(9)
    times = irregular_times(rng, 40, 60.0)
    values = rng.normal(size=times.size) + 0.03 * times
    a = trend(Series.from_arrays(times, values))
    b = trend(Series.from_arrays(times + 20_000.0, values))
    assert (a.s, a.var_s, a.z, a.p_value) == (b.s, b.var_s, b.z, b.p_value)
    assert a.slope == pytest.approx(b.slope, rel=1e-9)
    assert a.slope_low == pytest.approx(b.slope_low, rel=1e-9)


def test_the_time_unit_scales_the_slope_and_leaves_the_test_alone() -> None:
    rng = generator(10)
    times = np.unique(np.round(irregular_times(rng, 40, 60.0)))
    values = rng.normal(size=times.size) + 0.05 * times
    per_day = trend(Series.from_arrays(times, values))
    per_hour = trend(Series.from_arrays(times * 24.0, values))
    assert per_hour.s == per_day.s
    assert per_hour.p_value == pytest.approx(per_day.p_value, rel=1e-9)
    assert per_hour.slope == pytest.approx(per_day.slope / 24.0, rel=1e-9)
    assert per_hour.slope_high == pytest.approx(per_day.slope_high / 24.0, rel=1e-9)


@pytest.mark.parametrize(("scale", "offset"), [(2.5, 10.0), (0.1, -3.0), (-1.0, 0.0)])
def test_an_affine_change_of_the_values_carries_through(scale: float, offset: float) -> None:
    rng = generator(12)
    times = irregular_times(rng, 40, 60.0)
    values = rng.normal(size=times.size) + 0.04 * times
    a = trend(Series.from_arrays(times, values))
    b = trend(Series.from_arrays(times, scale * values + offset))
    assert b.s == (a.s if scale > 0 else -a.s)
    assert b.p_value == pytest.approx(a.p_value, rel=1e-9)
    assert b.slope == pytest.approx(scale * a.slope, rel=1e-9)


def test_sparse_and_dense_sampling_of_one_line_give_the_same_slope() -> None:
    line_at: Callable[[np.ndarray], np.ndarray] = lambda t: 70.0 + 0.2 * t  # noqa: E731
    sparse = np.array([0.0, 7.0, 30.0, 31.0, 90.0])
    dense = np.unique(np.concatenate([sparse, np.arange(1.0, 90.0, 2.5)]))
    a = trend(Series.from_arrays(sparse, line_at(sparse)))
    b = trend(Series.from_arrays(dense, line_at(dense)))
    assert a.slope == pytest.approx(0.2, rel=1e-12)
    assert b.slope == pytest.approx(0.2, rel=1e-12)
    assert a.intercept == pytest.approx(70.0)


def test_outliers_do_not_move_the_slope() -> None:
    rng = generator(14)
    times = np.arange(60.0)
    values = 80.0 - 0.1 * times + rng.normal(0.0, 0.2, size=60)
    clean = trend(Series.from_arrays(times, values))
    spoiled_values = values.copy()
    spoiled_values[[5, 17, 23, 40, 51]] += np.array([25.0, -30.0, 40.0, -20.0, 35.0])
    spoiled = trend(Series.from_arrays(times, spoiled_values))
    assert spoiled.slope == pytest.approx(clean.slope, abs=0.01)
    assert spoiled.direction is Direction.DECREASING


# --- irregular sampling and gaps ---------------------------------------------------------------


def test_gaps_and_uneven_sampling_do_not_bias_the_slope() -> None:
    estimates = []
    for seed in range(40):
        rng = generator(500 + seed)
        times = irregular_times(rng, 40, 180.0, with_gaps=True)
        values = 90.0 - 0.04 * times + rng.normal(0.0, 0.5, size=times.size)
        estimates.append(trend(Series.from_arrays(times, values)).slope)
    assert float(np.mean(estimates)) == pytest.approx(-0.04, abs=0.002)
    assert max(estimates) < -0.02
    assert min(estimates) > -0.06


# --- error rates: seeded simulations -----------------------------------------------------------


@cache
def p_values(correction: Correction, n: int, phi: float, runs: int, seed: int) -> np.ndarray:
    """The p-values of ``runs`` seeded series of ``n`` points: white noise (``phi`` 0) or AR(1)
    with no trend at all, on irregular times (the test uses the order, the detrending the
    times)."""
    rng = generator(seed)
    found = np.empty(runs)
    for i in range(runs):
        values = ar1(rng, n, phi) if phi else rng.normal(size=n)
        times = np.sort(rng.choice(np.arange(int(n * 1.6)), size=n, replace=False)).astype(float)
        found[i] = trend(Series.from_arrays(times, values), correction=correction).p_value
    return found


RUNS = 1000


def binomial_slack(level: float, runs: int = RUNS) -> float:
    """Three standard errors of an observed rate of a test with true rate ``level``."""
    return 3.0 * math.sqrt(level * (1.0 - level) / runs)


@pytest.mark.parametrize("correction", list(Correction))
@pytest.mark.parametrize("alpha", [0.05, 0.01])
def test_white_noise_is_flagged_at_no_more_than_the_stated_alpha(
    correction: Correction, alpha: float
) -> None:
    rate = float(np.mean(p_values(correction, 60, 0.0, RUNS, 20261031) < alpha))
    assert rate <= alpha + binomial_slack(alpha), (correction, alpha, rate)


@pytest.mark.parametrize("correction", list(Correction))
def test_white_noise_shows_no_trend_in_at_least_95_percent_of_runs_at_alpha_001(
    correction: Correction,
) -> None:
    # the literal reading of the acceptance criterion, at the stricter level where a seeded
    # sample of 1000 cannot land under 95 % by chance
    no_trend = float(np.mean(p_values(correction, 60, 0.0, RUNS, 20261031) >= 0.01))
    assert no_trend >= 0.95


def test_modified_mk_holds_the_level_better_than_plain_mk_on_autocorrelated_series() -> None:
    # AR(1) with phi = 0.3 and no trend, n = 100. The plain test sees a trend in about 15 % of
    # them at alpha = 0.05; the Hamed-Rao test in about 10 %: it removes about half of the excess
    # but, as published (only the significant lags are used), not all of it.
    alpha = 0.05
    plain = float(np.mean(p_values(Correction.NONE, 100, 0.3, RUNS, 20261031) < alpha))
    modified = float(np.mean(p_values(Correction.HAMED_RAO, 100, 0.3, RUNS, 20261031) < alpha))
    assert plain > 2.5 * alpha  # the problem is real
    assert modified - alpha < 0.6 * (plain - alpha)  # and the correction takes out over 40 %
    assert modified < 2.2 * alpha


def test_the_correction_helps_more_the_stronger_the_autocorrelation() -> None:
    alpha = 0.05
    plain = float(np.mean(p_values(Correction.NONE, 100, 0.6, RUNS, 20261031) < alpha))
    modified = float(np.mean(p_values(Correction.HAMED_RAO, 100, 0.6, RUNS, 20261031) < alpha))
    assert plain > 0.25
    assert modified < 0.6 * plain
    # not near alpha: Hamed-Rao under-corrects strong autocorrelation (docs/dev/analysis-methods.md)
    assert modified > 2 * alpha


# --- arguments, errors, result -----------------------------------------------------------------


def test_too_few_points_too_many_points_and_tied_times_are_refused() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        trend(line(2, [1.0, 2.0]))
    with pytest.raises(ValueError, match="at most"):
        trend(Series.from_arrays(np.arange(MAX_POINTS + 1.0), np.zeros(MAX_POINTS + 1)))
    with pytest.raises(ValueError, match="distinct times"):
        trend(Series.from_arrays([0.0, 1.0, 1.0, 2.0], [1.0, 2.0, 3.0, 4.0]))


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1, 1.5])
def test_alpha_must_be_a_probability(alpha: float) -> None:
    with pytest.raises(ValueError, match="alpha"):
        trend(line(5, [1, 2, 3, 4, 5]), alpha=alpha)


def test_the_result_carries_n_coverage_the_algorithm_version_and_is_json_safe() -> None:
    series = Series.from_arrays(np.arange(0.0, 40.0, 2.0), np.arange(20.0), window_days=60)
    result = trend(series)
    assert result.algo_version == ALGO_VERSION == "trend/1"
    assert trend_module.ALGO_VERSION == "trend/1"
    assert result.inputs.n == 20
    assert float(result.inputs.coverage) == pytest.approx(20 / 60)
    decoded = json.loads(json.dumps(result.as_dict()))
    assert decoded["direction"] == "increasing"
    assert decoded["slope_per_day"] == pytest.approx(0.5)
    assert decoded["correction"] == "hamed_rao"
    assert decoded["inputs"]["n"] == 20


def test_the_variance_helper_counts_ties_by_the_formula() -> None:
    assert mann_kendall_variance(np.arange(5.0)) == pytest.approx(50 / 3)
    assert mann_kendall_variance(np.array([1.0, 2.0, 2.0, 3.0, 3.0, 3.0])) == pytest.approx(
        426 / 18
    )
    assert mann_kendall_variance(np.array([5.0, 5.0, 5.0])) == 0.0
