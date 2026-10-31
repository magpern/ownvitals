"""PELT on autocorrelated noise: the long-run noise estimate. SYNTHETIC data only.

Issue #68. Before it, PELT took the noise level of independent noise, and 100-point AR(1) series
had a spurious change point in about 30 % of runs at phi 0.3 and 80 % at phi 0.5. The cost is now
scaled by the long-run standard deviation, estimated from lag-1 and lag-2 differences, which a
level shift hardly touches. The targets, tested here on fixed seeds and tabulated with a larger
run in ``docs/dev/analysis-methods.md``:

* at most 5 % of 100-point AR(1) series with phi up to 0.5 get a change point they do not have;
* a level shift of 2 long-run standard deviations in the middle of such a series is found, within
  a few points of where it is, in nearly every run.
"""

import json
import math
from functools import cache

import numpy as np
import pytest
from methods_builders import SYNTHETIC, ar1, generator

from ownvitals.analysis.methods import changepoint as changepoint_module
from ownvitals.analysis.methods.changepoint import (
    MAX_LAG,
    PHI_MARGIN,
    PHI_MAX,
    TRIM_CUTOFF,
    TRIM_VARIANCE,
    estimate_long_run,
    estimate_sigma,
    find_changes,
    fit_variogram,
    trimmed_variance,
)
from ownvitals.analysis.methods.series import Series

SEED = 20261031


def test_the_fixtures_are_labelled_synthetic() -> None:
    assert SYNTHETIC is True


# --- the estimate by hand ----------------------------------------------------------------------


def test_the_truncation_correction_is_that_of_a_normal_distribution_cut_at_three() -> None:
    # variance of a standard normal truncated to +-3: 1 - 2 * 3 * pdf(3) / (2 cdf(3) - 1)
    #   = 1 - 6 * 0.0044318 / 0.9973002 = 0.97334
    assert TRIM_CUTOFF == 3.0
    assert pytest.approx(0.97334, abs=1e-5) == TRIM_VARIANCE


def test_the_trimmed_variance_by_hand() -> None:
    # -2 -1 0 0 1 2 100: median 0; absolute deviations 2 1 0 0 1 2 100 have median 1, so the
    # robust standard deviation is 1.4826 and 3 of them is 4.45. 100 is left out. The rest has
    # mean 0 and variance (4 + 1 + 0 + 0 + 1 + 4)/6 = 1.6667, divided by 0.97334 for the cut.
    values = np.array([-2.0, -1.0, 0.0, 0.0, 1.0, 2.0, 100.0])
    assert trimmed_variance(values) == pytest.approx((10.0 / 6.0) / TRIM_VARIANCE)
    # more than half equal to the median: no scale, the plain variance (here of 0 0 0 0 0 5 5)
    tied = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 5.0, 5.0])
    assert trimmed_variance(tied) == pytest.approx(float(np.var(tied)))
    assert trimmed_variance(np.full(6, 3.0)) == 0.0


def test_the_variogram_fit_by_hand() -> None:
    # AR(1) with marginal variance g = 1 and phi = 0.5: v_h = 2 g (1 - phi^h) = 2 * (1 - 0.5^h)
    #   h = 1 .. 5:  1, 1.5, 1.75, 1.875, 1.9375
    # and the fit finds exactly phi = 0.5 (a point of the grid) and g = 1
    variances = 2.0 * (1.0 - 0.5 ** np.arange(1.0, 6.0))
    assert variances.tolist() == [1.0, 1.5, 1.75, 1.875, 1.9375]
    phi, g = fit_variogram(variances)
    assert phi == pytest.approx(0.5)
    assert g == pytest.approx(1.0)
    # independent noise: every lag has the variance 2 g, and phi = 0
    assert fit_variogram(np.full(5, 3.0)) == (0.0, pytest.approx(1.5))
    # a variogram that has not levelled off in 5 lags is limited to the top of the grid
    assert fit_variogram(np.array([1.0, 2.0, 3.0, 4.0, 5.0]))[0] == PHI_MAX


def test_the_long_run_estimate_is_the_variogram_fit_with_the_margin() -> None:
    y = ar1(generator(6), 80, 0.4)
    variances = np.array([trimmed_variance(y[h:] - y[:-h]) for h in range(1, MAX_LAG + 1)])
    fitted, g = fit_variogram(variances)
    phi = min(fitted + PHI_MARGIN / math.sqrt(80.0), PHI_MAX)
    estimate = estimate_long_run(y)
    assert estimate.phi == pytest.approx(phi)
    assert estimate.sigma == pytest.approx(math.sqrt(g * (1.0 + phi) / (1.0 - phi)))
    assert PHI_MARGIN == 1.0


def test_the_estimate_of_a_constant_series_and_of_very_short_ones() -> None:
    constant = estimate_long_run(np.full(20, 80.0))
    assert (constant.phi, constant.sigma) == (0.0, 0.0)
    # fewer than 4 points: the independent-noise estimate
    short = np.array([1.0, 3.0, 2.0])
    assert estimate_long_run(short).phi == 0.0
    assert estimate_long_run(short).sigma == estimate_sigma(short)
    assert estimate_long_run(np.array([5.0])).sigma == 0.0


def test_the_coefficient_is_never_above_the_ceiling() -> None:
    # a slow, smooth curve is "all persistence": the ratio of the differences' variances is
    # large, and the ceiling stops the long-run variance from running away
    y = np.sin(np.linspace(0.0, 3.0, 80)) + 0.01 * generator(1).normal(size=80)
    assert estimate_long_run(y).phi == PHI_MAX == 0.7


# --- properties --------------------------------------------------------------------------------


def test_scale_and_level_change_sigma_but_not_phi() -> None:
    y = ar1(generator(2), 150, 0.4)
    a, b = estimate_long_run(y), estimate_long_run(7.0 * y + 100.0)
    assert b.phi == pytest.approx(a.phi)
    assert b.sigma == pytest.approx(7.0 * a.sigma)


def test_a_level_shift_hardly_moves_the_estimate() -> None:
    # the differences touch a shift once; an 8 sd shift is beyond the cut-off in both of them
    y = generator(3).normal(size=400)
    shifted = y.copy()
    shifted[200:] += 8.0
    clean, with_shift = estimate_long_run(y), estimate_long_run(shifted)
    assert with_shift.sigma == pytest.approx(clean.sigma, rel=0.05)
    assert with_shift.phi == pytest.approx(clean.phi, abs=0.03)


def test_independent_noise_is_estimated_cautiously_not_wildly() -> None:
    # sigma 1: the estimate is a little above it (the margin and the noise of the estimate of phi
    # only ever add), never below, and phi stays small. docs/dev/analysis-methods.md: 1.2 to 1.4.
    sigmas = [estimate_long_run(generator(100 + i).normal(size=100)).sigma for i in range(60)]
    assert 1.0 < float(np.mean(sigmas)) < 1.5
    assert max(sigmas) < 2.5


def test_ar1_noise_gets_a_larger_long_run_sigma_than_its_own_standard_deviation() -> None:
    # AR(1), phi 0.5, innovations of sd 1: marginal sd 1.155, long-run sd 1/(1 - phi) = 2
    y = ar1(generator(4), 2000, 0.5)
    estimate = estimate_long_run(y)
    assert estimate.phi == pytest.approx(0.5, abs=0.15)
    assert estimate.sigma == pytest.approx(2.0, rel=0.3)
    assert estimate.sigma > 1.4 * float(np.std(y))


def test_the_result_says_which_coefficient_it_used_and_a_given_sigma_has_none() -> None:
    y = ar1(generator(5), 60, 0.3)
    estimated = find_changes(Series.from_arrays(np.arange(60.0), y))
    assert estimated.phi is not None
    assert 0.0 <= estimated.phi <= PHI_MAX
    assert estimated.sigma == pytest.approx(estimate_long_run(y).sigma)
    given = find_changes(Series.from_arrays(np.arange(60.0), y), sigma=2.0)
    assert given.phi is None
    assert given.sigma == 2.0
    assert json.loads(json.dumps(estimated.as_dict()))["phi"] == pytest.approx(estimated.phi)
    assert json.loads(json.dumps(given.as_dict()))["phi"] is None
    assert changepoint_module.ALGO_VERSION == "changepoint/2"


def test_a_shift_is_reported_in_long_run_standard_deviations() -> None:
    # 5 points at 0 and 5 at 6, with some noise: shift_sigmas = shift / sigma, sigma long-run
    y = np.concatenate([[0.2, -0.1, 0.0, 0.3, -0.2], [6.1, 5.8, 6.0, 6.3, 5.9]])
    result = find_changes(Series.from_arrays(np.arange(10.0), y))
    assert result.indices == (5,)
    change = result.change_points[0]
    assert change.shift_sigmas == pytest.approx(change.shift / result.sigma)


# --- the targets: seeded simulations -----------------------------------------------------------


@cache
def spurious_rate(n: int, phi: float, runs: int, seed: int, as_in_38: bool = False) -> float:
    """The share of ``runs`` AR(1) series of ``n`` points, with no change at all, that get a
    change point. With ``as_in_38`` the cost uses the noise level #38 estimated, the standard
    deviation of the differences over sqrt(2), instead of the long-run one."""
    rng = generator(seed)
    times = np.arange(float(n))
    found = 0
    for _ in range(runs):
        y = ar1(rng, n, phi)
        found += bool(
            find_changes(
                Series(times, y), sigma=estimate_sigma(y) if as_in_38 else None
            ).change_points
        )
    return found / runs


def test_autocorrelated_noise_gets_a_spurious_change_point_in_at_most_5_percent_of_100_points() -> (
    None
):
    # the target; measured with 1000 runs: 0.5 % at phi 0.3, 2.7 % at phi 0.5
    assert spurious_rate(100, 0.3, 250, SEED) <= 0.05
    assert spurious_rate(100, 0.5, 400, SEED) <= 0.05


def test_the_rate_for_independent_noise_is_unchanged_or_lower() -> None:
    # #38 measured 2.7 % at 60 points (97.3 % without); the same or fewer now
    assert spurious_rate(60, 0.0, 300, SEED) <= 0.03


def test_without_the_long_run_scale_autocorrelated_noise_is_the_problem_it_was() -> None:
    # the noise level #38 estimated, as sigma: most series get a change point
    assert spurious_rate(100, 0.5, 100, SEED, as_in_38=True) > 0.6  # about 80 % measured
    assert spurious_rate(100, 0.3, 100, SEED, as_in_38=True) > 0.15  # about 30 % measured


def test_shorter_series_are_held_less_tightly_and_this_is_known() -> None:
    # 30 points, phi 0.5: about 3 % measured with 600 runs. The estimate of phi from 28 differences
    # is noisy; the bound is the one the docs state for short series, not the 5 % target.
    assert spurious_rate(30, 0.5, 300, SEED) <= 0.08


@cache
def planted(n: int, phi: float, runs: int, seed: int) -> tuple[float, float, float]:
    """``(exactly one change point, one within 5 points of the planted one, more than two)`` as
    shares of ``runs`` AR(1) series with a shift of 2 long-run standard deviations at ``n // 2``."""
    rng = generator(seed)
    times = np.arange(float(n))
    at = n // 2
    long_run_sd = 1.0 / (1.0 - phi)  # innovations of sd 1
    exactly_one = near = many = 0
    for _ in range(runs):
        y = ar1(rng, n, phi)
        y[at:] += 2.0 * long_run_sd
        indices = find_changes(Series(times, y)).indices
        exactly_one += len(indices) == 1
        near += any(abs(i - at) <= 5 for i in indices)
        many += len(indices) > 2
    return exactly_one / runs, near / runs, many / runs


@pytest.mark.parametrize("phi", [0.0, 0.3, 0.5])
def test_a_shift_of_two_long_run_standard_deviations_is_found_where_it_is(phi: float) -> None:
    # measured with 200 runs: exactly one in 96-100 %, within 5 points in 94-100 %
    exactly_one, near, many = planted(100, phi, 120, SEED)
    assert near >= 0.88
    assert exactly_one >= 0.85
    assert many <= 0.03
