"""PELT change points and the minimum-data gate. SYNTHETIC data only.

PELT is checked three ways: hand-computed small cases (the cost and the penalty worked out on
paper), exact agreement with the O(n^2) optimal-partitioning recursion of Jackson et al. (2005)
that PELT (Killick, Fearnhead and Eckley 2012) prunes without changing its answer, and
simulations with a planted shift and with none.
"""

import json
import math
from datetime import timedelta
from itertools import pairwise

import numpy as np
import pytest
from methods_builders import START, SYNTHETIC, daily_values, generator, irregular_times

from ownvitals.analysis.methods import changepoint as changepoint_module
from ownvitals.analysis.methods.changepoint import (
    ALGO_VERSION,
    DEFAULT_MIN_SEGMENT,
    PENALTY_FACTOR,
    ChangePointResult,
    default_penalty,
    detect_changes,
    estimate_sigma,
    find_changes,
    pelt,
)
from ownvitals.analysis.methods.series import Series
from ownvitals.analysis.policy import (
    DEFAULT_POLICY,
    InsufficientData,
    Purpose,
    Requirement,
    Sufficient,
    evaluate_metric,
)


def test_the_fixtures_are_labelled_synthetic() -> None:
    assert SYNTHETIC is True


def steps(*levels: tuple[int, float]) -> np.ndarray:
    """``(count, level)`` blocks laid end to end."""
    return np.concatenate([np.full(count, level) for count, level in levels])


# --- hand-computed cases -----------------------------------------------------------------------


def test_a_clean_step_by_hand() -> None:
    # Five 0s then five 10s, sigma = 1 (the cost is the sum of squares about each segment's mean).
    # One segment: mean 5, cost 10 * 25 = 250. Split at 5: cost 0 + 0. With the default penalty
    # 3 ln 10 = 6.91 the split costs 6.91 against 250: one change point, at index 5.
    result = find_changes(
        Series.from_arrays(np.arange(10.0), steps((5, 0.0), (5, 10.0))), sigma=1.0
    )
    assert result.indices == (5,)
    assert result.cost == 0.0
    assert result.penalty == pytest.approx(3.0 * math.log(10))
    change = result.change_points[0]
    assert (change.time, change.previous_time) == (5.0, 4.0)
    assert (change.mean_before, change.mean_after, change.shift) == (0.0, 10.0, 10.0)
    assert change.shift_sigmas == 10.0


@pytest.mark.parametrize(("penalty", "expected"), [(2.4, (5,)), (2.6, ())])
def test_the_penalty_decides_at_the_boundary_by_hand(
    penalty: float, expected: tuple[int, ...]
) -> None:
    # Five 0s then five 1s, sigma = 1: one segment costs 10 * 0.5^2 = 2.5; two segments cost 0
    # plus the penalty. So a split pays off exactly when penalty < 2.5.
    result = find_changes(
        Series.from_arrays(np.arange(10.0), steps((5, 0.0), (5, 1.0))), sigma=1.0, penalty=penalty
    )
    assert result.indices == expected


def test_the_minimum_segment_length_by_hand() -> None:
    # Three 0s then nine 10s (n = 12). The step is after 3 points. With a minimum segment of 5 the
    # first segment cannot be 3 long, so the nearest allowed split is at 5:
    #   one segment: mean 7.5, cost 3 * 56.25 + 9 * 6.25 = 225
    #   split at 5:  0 0 0 10 10 (mean 4: 3 * 16 + 2 * 36 = 120) | 10 x 7 (0) = 120
    #   split at 7:  0 0 0 10 10 10 10 (mean 5.71: 98.0 + 73.5 = 171.5) | 10 x 5 (0) = 171.5
    # and 120 + the penalty 3 ln 12 = 7.45 beats 225, so the answer is 5. With 3 (or 1) it is 3.
    y = steps((3, 0.0), (9, 10.0))
    series = Series.from_arrays(np.arange(12.0), y)
    assert find_changes(series, sigma=1.0, min_segment=5).indices == (5,)
    assert find_changes(series, sigma=1.0, min_segment=3).indices == (3,)
    assert find_changes(series, sigma=1.0, min_segment=1).indices == (3,)


def test_the_default_penalty_and_segment_length_are_the_documented_ones() -> None:
    assert PENALTY_FACTOR == 3.0
    assert DEFAULT_MIN_SEGMENT == 5
    assert default_penalty(100) == pytest.approx(3.0 * math.log(100))


# --- PELT is exact: it matches the O(n^2) recursion --------------------------------------------


def optimal_partitioning(
    values: np.ndarray, penalty: float, min_segment: int
) -> tuple[tuple[int, ...], float]:
    """Jackson et al. (2005): F(t) = min over s of F(s) + cost(s, t) + penalty, no pruning."""
    n = len(values)
    centred = values - values.mean()

    def cost(a: int, b: int) -> float:
        part = centred[a:b]
        return float(np.sum((part - part.mean()) ** 2))

    best = [math.inf] * (n + 1)
    best[0] = -penalty
    previous = [0] * (n + 1)
    for t in range(min_segment, n + 1):
        for s in [0, *range(min_segment, t - min_segment + 1)]:
            if t - s < min_segment or math.isinf(best[s]):
                continue
            value = best[s] + cost(s, t) + penalty
            if value < best[t] - 1e-12:
                best[t], previous[t] = value, s
    points: list[int] = []
    end = n
    while previous[end] > 0:
        end = previous[end]
        points.append(end)
    points.reverse()
    return tuple(points), best[n] - len(points) * penalty


def random_case(seed: int) -> tuple[np.ndarray, float, int]:
    """A short series with 0-3 planted level shifts, a penalty and a minimum segment length."""
    rng = generator(seed)
    n = int(rng.integers(8, 45))
    minimum = int(rng.integers(2, 9))
    values = rng.normal(size=n)
    for _ in range(int(rng.integers(0, 4))):
        values[int(rng.integers(1, n)) :] += rng.normal() * rng.uniform(1.0, 5.0)
    return values, float(rng.uniform(0.5, 12.0)), minimum


# Seeds 0-29 are arbitrary. The others are cases found by searching 3000 seeds with the pruning
# of a candidate applied at once instead of `min_segment` steps late: there PELT returns a
# worse segmentation than the unpruned recursion. They pin the delay that keeps it exact.
EXACTNESS_SEEDS = [*range(30), 218, 224, 408, 489, 822, 1167, 1276, 1634, 2862, 2871]


@pytest.mark.parametrize("seed", EXACTNESS_SEEDS)
def test_pelt_returns_the_optimum_of_the_unpruned_recursion(seed: int) -> None:
    values, penalty, minimum = random_case(seed)
    n = len(values)
    points, cost = pelt(values, penalty=penalty, min_segment=minimum)
    want_points, want_cost = optimal_partitioning(values, penalty, minimum)
    assert cost + penalty * len(points) == pytest.approx(want_cost + penalty * len(want_points))
    assert cost == pytest.approx(want_cost)
    assert points == want_points  # ties between optima do not occur with continuous data
    assert all(b - a >= minimum for a, b in pairwise([0, *points, n]))


def test_pelt_with_nothing_to_find_or_no_data() -> None:
    assert pelt(np.zeros(0), penalty=5.0, min_segment=3) == ((), 0.0)
    assert pelt(np.array([1.0, 2.0]), penalty=5.0, min_segment=3) == ((), pytest.approx(0.5))
    with pytest.raises(ValueError, match="penalty"):
        pelt(np.zeros(5), penalty=0.0, min_segment=1)
    with pytest.raises(ValueError, match="min_segment"):
        pelt(np.zeros(5), penalty=1.0, min_segment=0)


# --- planted shifts and stationary series ------------------------------------------------------


def planted(seed: int, shift: float, n: int = 80, at: int = 40) -> Series:
    rng = generator(seed)
    values = rng.normal(0.0, 1.0, size=n)
    values[at:] += shift
    return Series.from_arrays(np.arange(float(n)), values)


def test_a_planted_mean_shift_is_found_within_a_tolerance() -> None:
    runs = 200
    exact_one = 0
    errors = []
    for seed in range(runs):
        result = find_changes(planted(seed, 3.0))
        if len(result.change_points) == 1:
            exact_one += 1
            errors.append(abs(result.change_points[0].index - 40))
    assert exact_one / runs >= 0.95
    assert max(errors) <= 4
    assert float(np.mean(errors)) < 1.0


def test_a_planted_shift_downwards_and_two_shifts_are_found_with_their_sizes() -> None:
    rng = generator(77)
    values = np.concatenate(
        [rng.normal(80.0, 1.0, 40), rng.normal(84.0, 1.0, 35), rng.normal(81.0, 1.0, 45)]
    )
    result = find_changes(Series.from_arrays(np.arange(120.0), values))
    assert len(result.change_points) == 2
    first, second = result.change_points
    assert abs(first.index - 40) <= 3
    assert abs(second.index - 75) <= 3
    assert first.shift == pytest.approx(4.0, abs=0.8)
    assert second.shift == pytest.approx(-3.0, abs=0.8)
    assert first.mean_after == second.mean_before
    assert first.shift_sigmas is not None
    assert first.shift_sigmas > 3.0
    assert second.shift_sigmas is not None
    assert second.shift_sigmas < -2.0


def test_a_stationary_series_has_no_change_point() -> None:
    runs = 300
    none = sum(
        not find_changes(
            Series.from_arrays(np.arange(60.0), generator(1000 + seed).normal(size=60))
        ).change_points
        for seed in range(runs)
    )
    assert none / runs >= 0.95


def test_a_constant_series_has_no_change_point() -> None:
    constant = find_changes(Series.from_arrays(np.arange(30.0), np.full(30, 80.0)))
    assert constant.change_points == ()
    assert constant.searched
    assert constant.sigma == 0.0


def test_a_step_in_heavily_tied_values_is_still_found() -> None:
    # the differences are almost all zero, so their MAD is zero: the fallback noise level is used
    series = Series.from_arrays(np.arange(60.0), steps((30, 80.0), (30, 81.0)))
    result = find_changes(series)
    assert result.sigma > 0
    assert result.indices == (30,)


# --- invariances -------------------------------------------------------------------------------


def test_the_scale_and_offset_of_the_values_do_not_change_the_change_points() -> None:
    series = planted(5, 3.0)
    base = find_changes(series).indices
    assert base
    for scale, offset in [(2.5, 10.0), (0.01, -3.0), (-1.0, 0.0)]:
        other = Series.from_arrays(series.t, scale * series.y + offset)
        assert find_changes(other).indices == base


def test_the_spacing_of_the_points_in_time_does_not_change_which_points_are_the_change() -> None:
    rng = generator(3)
    values = planted(8, 3.0, n=70, at=30).y
    even = find_changes(Series.from_arrays(np.arange(70.0), values))
    times = irregular_times(rng, 70, 400.0, with_gaps=True)
    assert times.size == 70
    uneven = find_changes(Series.from_arrays(times, values))
    assert uneven.indices == even.indices
    index = uneven.change_points[0].index
    assert uneven.change_points[0].time == times[index]
    assert uneven.change_points[0].previous_time == times[index - 1]


def test_the_input_order_does_not_matter() -> None:
    rng = generator(4)
    series = planted(9, 3.0)
    order = rng.permutation(series.n)
    shuffled = Series.from_arrays(series.t[order], series.y[order])
    assert find_changes(shuffled).indices == find_changes(series).indices


def test_irregular_sampling_and_gaps_neither_crash_nor_move_the_change() -> None:
    # the same 60 values observed on a very uneven calendar, with a hole where the level changes
    rng = generator(11)
    values = np.concatenate([rng.normal(0.0, 1.0, 30), rng.normal(4.0, 1.0, 30)])
    times = np.cumsum(rng.choice([0.2, 1.0, 1.0, 3.0, 12.0], size=60))
    times[30:] += 40.0  # a six-week hole between the two levels
    result = find_changes(Series.from_arrays(times, values))
    assert len(result.change_points) == 1
    change = result.change_points[0]
    assert abs(change.index - 30) <= 2
    assert change.time - change.previous_time >= 1.0


def test_every_segment_is_at_least_min_segment_long() -> None:
    rng = generator(13)
    values = rng.normal(size=100)
    values[20:] += 2.0
    values[55:] -= 2.5
    series = Series.from_arrays(np.arange(100.0), values)
    for minimum in (2, 5, 12, 30):
        result = find_changes(series, min_segment=minimum, penalty=2.0)
        edges = [0, *result.indices, 100]
        assert all(b - a >= minimum for a, b in pairwise(edges)), minimum


def test_too_short_a_series_is_not_searched() -> None:
    short = find_changes(Series.from_arrays(np.arange(9.0), steps((4, 0.0), (5, 10.0))))
    assert not short.searched
    assert short.change_points == ()
    assert find_changes(Series.from_arrays([], [])).change_points == ()
    long_enough = find_changes(Series.from_arrays(np.arange(10.0), steps((5, 0.0), (5, 10.0))))
    assert long_enough.searched


def test_a_long_series_with_a_few_shifts_is_handled() -> None:
    rng = generator(21)
    values = rng.normal(size=1500)
    values[400:] += 1.5
    values[1100:] -= 2.0
    result = find_changes(Series.from_arrays(np.arange(1500.0), values))
    assert len(result.change_points) == 2
    assert abs(result.indices[0] - 400) <= 5
    assert abs(result.indices[1] - 1100) <= 5


# --- noise level and arguments -----------------------------------------------------------------


def test_the_noise_level_estimate_is_robust_to_a_level_shift() -> None:
    rng = generator(2)
    noise = rng.normal(0.0, 1.0, size=400)
    with_shift = noise.copy()
    with_shift[200:] += 8.0
    assert estimate_sigma(noise) == pytest.approx(1.0, abs=0.12)
    assert estimate_sigma(with_shift) == pytest.approx(estimate_sigma(noise), abs=0.05)
    assert estimate_sigma(np.full(10, 3.0)) == 0.0
    assert estimate_sigma(np.array([1.0])) == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [{"sigma": 0.0}, {"sigma": -1.0}, {"penalty": 0.0}, {"penalty": -2.0}, {"min_segment": 0}],
)
def test_bad_arguments_are_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"positive|at least"):
        find_changes(Series.from_arrays(np.arange(20.0), np.arange(20.0)), **kwargs)  # type: ignore[arg-type]


def test_the_result_carries_n_coverage_and_the_algorithm_version_and_is_json_safe() -> None:
    series = Series.from_arrays(np.arange(10.0), steps((5, 0.0), (5, 10.0)), window_days=20)
    result = find_changes(series, sigma=1.0)
    assert result.algo_version == ALGO_VERSION == "changepoint/1"
    assert result.inputs.n == 10
    assert float(result.inputs.coverage) == 0.5
    decoded = json.loads(json.dumps(result.as_dict()))
    assert decoded["change_points"][0]["index"] == 5
    assert decoded["data_quality"] is None
    constant = find_changes(Series.from_arrays(np.arange(20.0), np.ones(20)))
    assert json.loads(json.dumps(constant.as_dict()))["change_points"] == []


# --- the minimum-data gate ---------------------------------------------------------------------


def sufficient_daily(shift: float = 4.0, days: int = 60) -> list:
    """60 consecutive days, level 80 and then 80 + shift from day 30."""
    rng = generator(31)
    values = np.round(80.0 + rng.normal(0.0, 0.5, size=days), 2)
    values[30:] += shift
    return daily_values(list(range(days)), [float(v) for v in values])


def test_a_series_that_passes_the_policy_is_searched_and_carries_its_data_quality() -> None:
    daily = sufficient_daily()
    end = START + timedelta(days=59)
    result = detect_changes(daily, metric_key="weight", window_end=end, window_days=60)
    assert isinstance(result, ChangePointResult)
    assert len(result.change_points) == 1
    assert abs(result.change_points[0].index - 30) <= 2
    verdict = evaluate_metric("weight", Purpose.TREND, daily, window_end=end, window_days=60)
    assert isinstance(verdict, Sufficient)
    assert result.data_quality == verdict.data_quality
    assert result.inputs.n == 60
    assert float(result.inputs.coverage) == 1.0
    assert json.loads(json.dumps(result.as_dict()))["data_quality"]["n_days"] == 60


def test_a_series_that_fails_the_policy_is_not_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("PELT must not run on insufficient data")

    monkeypatch.setattr(changepoint_module, "pelt", explode)
    # 6 days with data in a 60-day window: below the trend minimum of 8 days
    daily = daily_values([0, 10, 20, 30, 40, 59], [80.0, 80.5, 81.0, 85.0, 85.5, 86.0])
    end = START + timedelta(days=59)
    result = detect_changes(daily, metric_key="weight", window_end=end, window_days=60)
    assert isinstance(result, InsufficientData)
    assert result == evaluate_metric("weight", Purpose.TREND, daily, window_end=end, window_days=60)
    assert result.n == 6


def test_days_outside_the_window_are_ignored() -> None:
    daily = sufficient_daily(days=60)
    end = START + timedelta(days=59)
    # a 30-day window keeps days 30..59: one level only -> sufficient, nothing to find
    result = detect_changes(daily, metric_key="weight", window_end=end, window_days=30)
    assert isinstance(result, ChangePointResult)
    assert result.inputs.n == 30
    assert result.change_points == ()


def test_the_purpose_the_metric_and_an_explicit_requirement_choose_the_threshold() -> None:
    daily = daily_values(list(range(0, 40, 4)), [80.0 + (i // 5) * 4.0 for i in range(10)])
    end = START + timedelta(days=39)
    # ten days with data over a 37-day span in a 40-day window
    assert isinstance(
        detect_changes(daily, metric_key="weight", window_end=end, window_days=40),
        ChangePointResult,
    )
    baseline = detect_changes(
        daily, metric_key="weight", window_end=end, window_days=40, purpose=Purpose.BASELINE
    )
    assert isinstance(baseline, ChangePointResult)
    strict = Requirement(min_days_with_data=11)
    refused = detect_changes(
        daily, metric_key="weight", window_end=end, window_days=40, requirement=strict
    )
    assert isinstance(refused, InsufficientData)
    assert refused.requirement == strict
    assert DEFAULT_POLICY.requirement(Purpose.TREND).min_days_with_data == 8


def test_a_nerve_metric_passes_its_own_policy_but_is_too_short_to_search() -> None:
    # N1: 4 measures over 28 days elapsed (day 0 to day 28) are enough for a trend, and far too
    # few for a change point
    daily = daily_values([0, 10, 19, 28], [50.0, 52.0, 51.0, 49.0])
    end = START + timedelta(days=28)
    result = detect_changes(daily, metric_key="nerve_health_score", window_end=end, window_days=29)
    assert isinstance(result, ChangePointResult)
    assert not result.searched
    assert result.change_points == ()
    assert result.data_quality is not None
    assert result.data_quality.n_days == 4
