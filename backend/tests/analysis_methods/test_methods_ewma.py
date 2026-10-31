"""EWMA weighted by the time gap. SYNTHETIC data only.

Reference values are worked out by hand from the recurrence in the module docstring
(``alpha_i = 1 - 2^(-gap_i / halflife)``, ``E_i = alpha_i * y_i + (1 - alpha_i) * E_(i-1)``,
``E_0 = y_0``), the exponential average of an unevenly spaced series with next-point
interpolation of Eckner (2019), section 3.1.
"""

import json

import numpy as np
import pytest
from methods_builders import SYNTHETIC, generator, irregular_times

from ownvitals.analysis.methods.ewma import ALGO_VERSION, ewma, gap_weight
from ownvitals.analysis.methods.series import Series


def test_the_fixtures_are_labelled_synthetic() -> None:
    assert SYNTHETIC is True


# --- hand-computed references -----------------------------------------------------------------


def test_three_irregular_points_by_hand() -> None:
    # halflife 1 day. t = 0, 1, 3 and y = 10, 20, 40.
    # alpha_1 = 1 - 0.5^1 = 0.5   -> E_1 = 0.5 * 20 + 0.5 * 10 = 15
    # alpha_2 = 1 - 0.5^2 = 0.75  -> E_2 = 0.75 * 40 + 0.25 * 15 = 33.75
    result = ewma(Series.from_arrays([0.0, 1.0, 3.0], [10.0, 20.0, 40.0]), halflife_days=1.0)
    assert result.alpha.tolist() == [1.0, 0.5, 0.75]
    assert result.ewma.tolist() == [10.0, 15.0, 33.75]


def test_a_reading_one_halflife_after_the_last_weighs_one_half() -> None:
    for halflife in (0.5, 3.0, 14.0):
        result = ewma(Series.from_arrays([0.0, halflife], [0.0, 1.0]), halflife_days=halflife)
        assert result.ewma[1] == pytest.approx(0.5)
        assert gap_weight(np.array([halflife]), halflife)[0] == pytest.approx(0.5)


def test_equal_spacing_is_the_classic_fixed_factor_ewma() -> None:
    rng = generator(3)
    values = rng.normal(80.0, 1.0, size=40)
    series = Series.from_arrays(np.arange(40.0), values)
    result = ewma(series, halflife_days=4.0)
    alpha = 1.0 - 0.5 ** (1.0 / 4.0)
    expected = [values[0]]
    for v in values[1:]:
        expected.append(alpha * v + (1.0 - alpha) * expected[-1])
    np.testing.assert_allclose(result.ewma, expected, rtol=1e-12)


def test_a_long_gap_forgets_the_past() -> None:
    # 100 halflives of silence: the weight of the new reading is 1 - 2^-100
    result = ewma(Series.from_arrays([0.0, 1.0, 101.0], [50.0, 60.0, 90.0]), halflife_days=1.0)
    assert result.ewma[2] == pytest.approx(90.0, abs=1e-12)


def test_the_weight_of_a_reading_depends_on_the_gap_only() -> None:
    gap = 5.0
    for start in (0.0, 3.0, 400.0):
        result = ewma(Series.from_arrays([start, start + gap], [1.0, 2.0]), halflife_days=2.0)
        assert result.alpha[1] == pytest.approx(1.0 - 0.5 ** (gap / 2.0))
    # and not on how many readings came before
    many = ewma(Series.from_arrays(np.arange(0.0, 50.0, 1.0), np.ones(50)), halflife_days=2.0)
    assert many.alpha[1:].tolist() == [pytest.approx(1.0 - 0.5**0.5)] * 49


# --- properties: the decay is in time, not in points ------------------------------------------


def test_splitting_a_gap_with_points_that_repeat_the_next_value_changes_nothing() -> None:
    # sparse: 0 at day 0, then 1 on days 10 and 20. Dense: the days in between repeat the value
    # of the next reading. Same average at the common days.
    sparse = Series.from_arrays([0.0, 10.0, 20.0], [0.0, 1.0, 1.0])
    dense_t = np.arange(0.0, 21.0)
    dense = Series.from_arrays(dense_t, np.where(dense_t == 0.0, 0.0, 1.0))
    a = ewma(sparse, halflife_days=5.0)
    b = ewma(dense, halflife_days=5.0)
    # E(10) = 1 - 2^-2 * 1 = 0.75 in both
    assert a.ewma[1] == pytest.approx(0.75)
    assert b.ewma[10] == pytest.approx(0.75)
    assert b.ewma[20] == pytest.approx(a.ewma[2])


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_adding_points_that_repeat_the_next_value_changes_nothing(seed: int) -> None:
    # (1 - alpha(d1)) * (1 - alpha(d2)) = 1 - alpha(d1 + d2): a point inserted between two others
    # with the value of the later one splits a gap without adding information.
    rng = generator(seed)
    times = irregular_times(rng, 40, 80.0)
    values = rng.normal(size=times.size)
    plain = ewma(Series.from_arrays(times, values), halflife_days=6.0)

    extra_t: list[float] = []
    extra_y: list[float] = []
    for i in range(1, times.size):
        for fraction in rng.uniform(0.05, 0.95, size=int(rng.integers(0, 4))):
            extra_t.append(float(times[i - 1] + fraction * (times[i] - times[i - 1])))
            extra_y.append(float(values[i]))
    dense = ewma(
        Series.from_arrays(np.concatenate([times, extra_t]), np.concatenate([values, extra_y])),
        halflife_days=6.0,
    )
    keep = np.isin(dense.t, times)
    assert dense.t.size > times.size
    np.testing.assert_allclose(dense.ewma[keep], plain.ewma, rtol=1e-12, atol=1e-12)


def test_the_time_unit_does_not_matter_when_the_halflife_is_converted_with_it() -> None:
    rng = generator(7)
    times = np.unique(np.round(irregular_times(rng, 50, 100.0)))
    values = rng.normal(size=times.size)
    days = ewma(Series.from_arrays(times, values), halflife_days=5.0)
    hours = ewma(Series.from_arrays(times * 24.0, values), halflife_days=5.0 * 24.0)
    np.testing.assert_allclose(days.ewma, hours.ewma, rtol=1e-12)


def test_shifting_the_times_does_not_change_the_average() -> None:
    rng = generator(8)
    times = irregular_times(rng, 50, 100.0)
    values = rng.normal(size=times.size)
    a = ewma(Series.from_arrays(times, values), halflife_days=5.0)
    b = ewma(Series.from_arrays(times + 20_000.0, values), halflife_days=5.0)
    np.testing.assert_allclose(a.ewma, b.ewma, rtol=1e-9)


def test_the_average_stays_between_the_extremes_and_a_constant_series_is_unchanged() -> None:
    rng = generator(9)
    times = irregular_times(rng, 60, 90.0, with_gaps=True)
    values = rng.normal(80.0, 2.0, size=times.size)
    result = ewma(Series.from_arrays(times, values), halflife_days=7.0)
    assert result.ewma.min() >= values.min() - 1e-12
    assert result.ewma.max() <= values.max() + 1e-12
    flat = ewma(Series.from_arrays(times, np.full(times.size, 81.3)), halflife_days=7.0)
    np.testing.assert_allclose(flat.ewma, 81.3, rtol=1e-14)


def test_a_longer_halflife_smooths_more() -> None:
    rng = generator(12)
    values = rng.normal(size=200)
    series = Series.from_arrays(np.arange(200.0), values)
    spreads = [float(np.std(ewma(series, halflife_days=h).ewma)) for h in (1.0, 5.0, 25.0)]
    assert spreads[0] > spreads[1] > spreads[2]


# --- irregular sampling, gaps, ties -----------------------------------------------------------


def test_gaps_and_clusters_do_not_crash_and_a_dense_period_does_not_dominate() -> None:
    # a week of 20 readings per day at 80, three silent weeks, then one reading at 84.
    cluster = np.repeat(np.arange(7.0), 20) + np.tile(np.linspace(0.0, 0.9, 20), 7)
    times = np.concatenate([cluster, [28.0]])
    values = np.concatenate([np.full(cluster.size, 80.0), [84.0]])
    result = ewma(Series.from_arrays(times, values), halflife_days=3.0)
    # the last cluster reading is at 6.9; 21.1 days later 2^(-21.1/3) of the past is left, so the
    # new reading counts almost fully however many readings the cluster held
    left = 0.5 ** ((28.0 - cluster[-1]) / 3.0)
    assert result.ewma[-1] == pytest.approx(84.0 - 4.0 * left, abs=1e-9)
    assert left < 0.01


def test_points_at_the_same_time_are_merged_and_the_order_does_not_matter() -> None:
    a = ewma(Series.from_arrays([0.0, 1.0, 1.0, 2.0], [10.0, 20.0, 40.0, 30.0]), halflife_days=1.0)
    b = ewma(Series.from_arrays([2.0, 1.0, 0.0, 1.0], [30.0, 40.0, 10.0, 20.0]), halflife_days=1.0)
    np.testing.assert_array_equal(a.ewma, b.ewma)
    # the two points on day 1 count as their mean, 30: E_1 = 0.5 * 30 + 0.5 * 10 = 20
    assert a.ewma[1] == a.ewma[2] == 20.0
    assert a.ewma[3] == pytest.approx(0.5 * 30.0 + 0.5 * 20.0)


def test_a_single_point_and_the_empty_series() -> None:
    one = ewma(Series.from_arrays([3.0], [7.0]), halflife_days=2.0)
    assert one.ewma.tolist() == [7.0]
    assert one.alpha.tolist() == [1.0]
    empty = ewma(Series.from_arrays([], []), halflife_days=2.0)
    assert empty.ewma.size == 0
    assert empty.inputs.n == 0


def test_the_result_carries_n_coverage_and_the_algorithm_version() -> None:
    series = Series.from_arrays([0.0, 2.0, 9.0], [1.0, 2.0, 3.0], window_days=30)
    result = ewma(series, halflife_days=3.0)
    assert result.algo_version == ALGO_VERSION == "ewma/1"
    assert result.inputs.n == 3
    assert float(result.inputs.coverage) == pytest.approx(0.1)
    assert json.loads(json.dumps(result.as_dict()))["halflife_days"] == 3.0


@pytest.mark.parametrize("halflife", [0.0, -1.0, float("nan")])
def test_a_bad_halflife_is_refused(halflife: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        ewma(Series.from_arrays([0.0, 1.0], [1.0, 2.0]), halflife_days=halflife)
