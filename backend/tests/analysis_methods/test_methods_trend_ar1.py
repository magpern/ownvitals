"""The AR(1) correction of the Mann-Kendall variance (``Correction.AR1``). SYNTHETIC data only.

Checked three ways: hand-computed values of the factor and of the coefficient, properties (the
correction only ever inflates, ignores the scale of the values, is not moved by an outlier), and
seeded simulations of the rate at which a series with no trend at all is called a trend. The
simulations are the acceptance test of issue #68: at alpha = 0.05 the rate of AR(1) series with
phi 0.3 and 0.6 must stay within alpha plus three binomial standard errors of the runs made, at
the lengths the minimum-data policy lets through (8 days, the smallest, up to a year of daily
values). The rates are tabulated, from 40,000 series per cell, in ``docs/dev/analysis-methods.md``:
at phi 0.6 and fewer than 20 points the true rate is about 6 %, not 5 %.
"""

import math

import numpy as np
import pytest
from methods_builders import (
    RUNS,
    SYNTHETIC,
    ar1,
    binomial_slack,
    generator,
    null_trend_series,
    plain_and_ar1_p_values,
    plain_p_value,
)

from ownvitals.analysis.methods.series import Series
from ownvitals.analysis.methods.trend import (
    AR1_MAX,
    Correction,
    ar1_coefficient,
    ar1_factor,
    trend,
)

SEED = 20261031
ALPHA = 0.05


def test_the_fixtures_are_labelled_synthetic() -> None:
    assert SYNTHETIC is True


# --- the factor n/n* by hand -------------------------------------------------------------------


def test_the_factor_by_hand() -> None:
    # n/n* = 1 + 2 / (n(n-1)(n-2)) * sum_k (n-k)(n-k-1)(n-k-2) phi^k
    # n = 5, phi = 0.5: k = 1 gives 4*3*2 = 24 -> 24 * 0.5 = 12; k = 2 gives 3*2*1 = 6 -> 6 * 0.25
    # = 1.5; k = 3 gives 2*1*0 = 0. So 1 + 2 * 13.5 / 60 = 1.45.
    assert ar1_factor(5, 0.5) == pytest.approx(1.45, abs=1e-12)
    # n = 4, phi = 0.8: only k = 1 counts, 3*2*1 = 6 -> 1 + 2 * 6 * 0.8 / 24 = 1.4
    assert ar1_factor(4, 0.8) == pytest.approx(1.4, abs=1e-12)
    # n = 3: every weight is zero. phi = 0: no autocorrelation, nothing to correct.
    assert ar1_factor(3, 0.9) == 1.0
    assert ar1_factor(40, 0.0) == 1.0
    with pytest.raises(ValueError, match="at least 3"):
        ar1_factor(2, 0.5)


def test_the_factor_matches_a_plain_loop() -> None:
    n, phi = 30, 0.6
    total = 0.0
    for k in range(1, n):
        total += (n - k) * (n - k - 1) * (n - k - 2) * phi**k
    assert ar1_factor(n, phi) == pytest.approx(1.0 + 2.0 * total / (n * (n - 1) * (n - 2)))
    # it grows with phi and with n, and stays below its limit (1 + phi)/(1 - phi) = 4
    assert ar1_factor(30, 0.3) < ar1_factor(30, 0.6) < ar1_factor(30, 0.9)
    assert ar1_factor(30, 0.6) < ar1_factor(300, 0.6) < 4.0


def test_the_factor_tends_to_the_effective_sample_size_of_an_ar1_series() -> None:
    # for long series n/n* tends to (1 + phi)/(1 - phi) (Bayley and Hammersley 1946)
    assert ar1_factor(3000, 0.5) == pytest.approx(3.0, rel=5e-3)
    assert ar1_factor(3000, 0.8) == pytest.approx(9.0, rel=5e-3)


# --- the coefficient by hand -------------------------------------------------------------------


def test_the_coefficient_by_hand() -> None:
    # Ranks 1 3 6 2 5 4 (n = 6): centred at 3.5 they are -2.5 -0.5 2.5 -1.5 1.5 0.5, the sum of
    # squares is 17.5 and the sum of the lag-1 products is 1.25 - 1.25 - 3.75 - 2.25 + 0.75
    # = -5.25, so r_1 = -0.3. On the Gaussian scale r = 2 sin(-0.3 pi / 6) = -0.312869 and with
    # n = 6 the bias and margin term is (6 + 6 r) / 6 = 1 + r, so phi = 1 + 2 r = 0.374262.
    r, phi = ar1_coefficient(np.array([1.0, 3.0, 6.0, 2.0, 5.0, 4.0]))
    assert r == pytest.approx(2.0 * math.sin(-0.3 * math.pi / 6.0), abs=1e-12)
    assert r == pytest.approx(-0.312869, abs=1e-6)
    assert phi == pytest.approx(1.0 + 2.0 * r, abs=1e-12)
    assert phi == pytest.approx(0.374262, abs=1e-6)


def test_the_coefficient_is_limited_to_zero_and_the_ceiling() -> None:
    # ranks 1..10 in order: r_1 = 0.7 (see the Hamed-Rao test), r = 0.716736, and the term
    # (6 + 6 r)/10 = 1.03 takes it far above the ceiling
    r, phi = ar1_coefficient(np.arange(1.0, 11.0))
    assert r == pytest.approx(0.716736, abs=1e-6)
    assert phi == AR1_MAX == 0.95
    # strongly alternating ranks: r_1 about -0.93, below zero even with the added term
    alternating = np.array(
        [1, 20, 2, 19, 3, 18, 4, 17, 5, 16, 6, 15, 7, 14, 8, 13, 9, 12, 10, 11.0]
    )
    r, phi = ar1_coefficient(alternating)
    assert r < -0.9
    assert phi == 0.0


def test_a_series_with_no_autocorrelation_gets_only_the_margin() -> None:
    # tied ranks (a constant series) have r = 0; phi is then (6 + 0)/n, the margin of a short series
    assert ar1_coefficient(np.full(60, 5.0)) == (0.0, pytest.approx(0.1))
    assert ar1_coefficient(np.full(30, 5.0)) == (0.0, pytest.approx(0.2))
    with pytest.raises(ValueError, match="at least 3"):
        ar1_coefficient(np.array([1.0, 2.0]))


# --- properties --------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_the_correction_only_inflates_and_leaves_the_slope_alone(seed: int) -> None:
    rng = generator(seed)
    series = null_trend_series(rng, 20 + 15 * seed, 0.1 * seed)
    plain = trend(series, correction=Correction.NONE)
    corrected = trend(series)
    assert corrected.correction is Correction.AR1
    assert corrected.ar1_phi is not None
    assert 0.0 <= corrected.ar1_phi <= AR1_MAX
    assert corrected.n_over_n_star >= 1.0
    assert corrected.var_s_used >= plain.var_s
    assert corrected.var_s == plain.var_s
    assert (corrected.s, corrected.tau, corrected.slope) == (plain.s, plain.tau, plain.slope)
    assert corrected.p_value >= plain.p_value
    assert corrected.slope_low <= plain.slope_low
    assert corrected.slope_high >= plain.slope_high
    assert corrected.significant_lags == ()


def test_the_scale_and_the_level_of_the_values_change_nothing_but_the_slope() -> None:
    rng = generator(11)
    series = null_trend_series(rng, 50, 0.5)
    other = Series.from_arrays(series.t, 80.0 + 3.0 * series.y)
    a, b = trend(series), trend(other)
    assert (a.ar1_r, a.ar1_phi, a.n_over_n_star, a.p_value) == pytest.approx(
        (b.ar1_r, b.ar1_phi, b.n_over_n_star, b.p_value)
    )
    assert b.slope == pytest.approx(3.0 * a.slope)


def test_the_time_unit_changes_nothing() -> None:
    rng = generator(12)
    series = null_trend_series(rng, 50, 0.5)
    other = Series.from_arrays(series.t * 7.0 + 100.0, series.y)
    a, b = trend(series), trend(other)
    assert (a.ar1_r, a.ar1_phi, a.p_value) == pytest.approx((b.ar1_r, b.ar1_phi, b.p_value))


def test_one_wild_value_barely_moves_the_coefficient() -> None:
    # ranks bound the influence of an outlier: a reading of 1e6 shifts one rank by at most n
    rng = generator(13)
    values = ar1(rng, 100, 0.5)
    times = np.arange(100.0)
    clean = trend(Series.from_arrays(times, values))
    wild = values.copy()
    wild[50] = 1e6
    dirty = trend(Series.from_arrays(times, wild))
    assert clean.ar1_phi is not None
    assert dirty.ar1_phi is not None
    assert abs(dirty.ar1_phi - clean.ar1_phi) < 0.1
    assert abs(dirty.slope - clean.slope) < 0.01


def test_a_real_trend_is_found_through_the_correction_and_the_noise_around_it_is_measured() -> None:
    rng = generator(14)
    times = np.arange(120.0)
    values = 80.0 - 0.05 * times + ar1(rng, 120, 0.5)
    result = trend(Series.from_arrays(times, values))
    assert result.significant
    assert result.slope == pytest.approx(-0.05, abs=0.02)
    assert result.ar1_phi is not None
    assert 0.3 < result.ar1_phi < 0.9  # the trend was removed first, so the noise is what is left


def test_plain_p_value_is_that_of_the_none_correction() -> None:
    # the simulations below get both p-values from one call: S and Var(S) of the result
    rng = generator(15)
    for i in range(20):
        series = null_trend_series(rng, 12 + 7 * i, 0.4)
        result = trend(series)
        plain = trend(series, correction=Correction.NONE)
        assert plain_p_value(result) == pytest.approx(plain.p_value)


# --- the target: error rates of series with no trend -------------------------------------------

# (n, phi, runs). 8 is the smallest number of days with data the default trend policy accepts and
# a year of daily values the longest window. The runs are fewer where a call is slow: the bound
# below uses the standard error of the runs that were made.
#
# The bound is alpha plus three standard errors of that number of runs (7.07 % at 1000), not a
# claim about the true rate. docs/dev/analysis-methods.md has the true rates from 40,000 series
# per cell: phi 0.3 is 3.1 to 3.9 % at 8 to 20 points, but phi 0.6 is 5.9 % at 8 and 15 points
# and up to 6.4 % at 10 and 12 (4.3 to 4.8 % from 60 points on). That is about one point above
# alpha, and the bound shrinks as the runs grow: for these two cells (8 and 15 points, phi 0.6)
# it holds with a margin of about one point at 1000 runs (the seeded runs give 6.4 % and 5.4 %),
# so they guard against a regression of the margin, and must not be given more than about 2000
# runs without a new look at the bound. It is not loosened to describe the larger run.
CELLS = [
    (8, 0.3, RUNS),
    (8, 0.6, RUNS),
    (15, 0.3, RUNS),
    (15, 0.6, RUNS),
    (100, 0.3, 500),
    (100, 0.6, 500),
    (365, 0.6, 300),
]


@pytest.mark.parametrize(("n", "phi", "runs"), CELLS)
def test_autocorrelated_series_are_flagged_within_alpha_plus_three_standard_errors(
    n: int, phi: float, runs: int
) -> None:
    plain_p, corrected_p = plain_and_ar1_p_values(n, phi, runs, SEED)
    plain = float(np.mean(plain_p < ALPHA))
    rate = float(np.mean(corrected_p < ALPHA))
    # the target: alpha plus three binomial standard errors
    assert rate <= ALPHA + binomial_slack(ALPHA, runs), (n, phi, rate)
    # and a floor, so that staying under it is not just a test that never rejects
    assert rate >= ALPHA / 4.0, (n, phi, rate)
    # the problem is real and the correction is what removes it
    assert plain > rate
    if n >= 15 and phi >= 0.6:
        assert plain > 4.0 * ALPHA, (n, phi, plain)


def test_the_correction_is_needed_and_enough_at_the_length_of_the_baseline() -> None:
    # n = 100, phi 0.3: plain 14.3 %, Hamed-Rao 9.2 %, here 4.3 %: the documented table (40,000
    # series)
    plain_p, corrected_p = plain_and_ar1_p_values(100, 0.3, 500, SEED)
    assert float(np.mean(plain_p < ALPHA)) > 2.0 * ALPHA
    assert 0.02 <= float(np.mean(corrected_p < ALPHA)) <= ALPHA + binomial_slack(ALPHA, 500)


def power(n: int, drift: float, runs: int, seed: int) -> tuple[float, float]:
    """Plain and AR(1)-corrected detection rate of white noise plus a straight trend whose total
    rise over the series is ``drift`` standard deviations."""
    rng = generator(seed)
    plain = corrected = 0
    for _ in range(runs):
        null = null_trend_series(rng, n, 0.0)
        rise = drift * (null.t - null.t[0]) / (null.t[-1] - null.t[0])
        result = trend(Series.from_arrays(null.t, null.y + rise))
        plain += plain_p_value(result) < ALPHA
        corrected += result.significant
    return plain / runs, corrected / runs


def test_with_independent_noise_the_correction_costs_little_power_at_the_usual_length() -> None:
    # the independent-noise behaviour of #38 within a tolerance: a trend of 1.6 standard
    # deviations over 60 points is found by plain Mann-Kendall in 92.9 % of the series and by the
    # corrected test in 88.8 %. (At 20 points the margin that holds the level at phi 0.6 costs
    # more: a trend of 2.5 standard deviations is found in 85.0 % against 65.5 %. Both are from the
    # one run of 40,000 series per cell in docs/dev/analysis-methods.md.)
    plain, corrected = power(60, 1.6, 600, SEED)
    assert plain > 0.85
    assert corrected >= 0.9 * plain
