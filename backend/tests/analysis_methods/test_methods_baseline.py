"""Robust baseline: rolling median and MAD over time windows. SYNTHETIC data only.

Reference values are worked out by hand from the definitions in the module docstring: the median
of the values whose time is in the window, the raw MAD about it, scale = 1.4826 * MAD
(Rousseeuw and Croux 1993) and the typical range median +- k * scale.
"""

import json
import math

import numpy as np
import pytest
from methods_builders import SYNTHETIC, generator, irregular_times

from ownvitals.analysis.methods.baseline import (
    ALGO_VERSION,
    Window,
    rolling_baseline,
)
from ownvitals.analysis.methods.series import MAD_SCALE, Series

# t = 0..9; one spike (30) at t = 3
HAND = Series.from_arrays(
    np.arange(10.0), [10.0, 12.0, 11.0, 30.0, 12.0, 11.0, 13.0, 10.0, 12.0, 11.0], window_days=10
)


def at(series: Series, q: float, window_days: float, window: Window) -> tuple[float, ...]:
    result = rolling_baseline(series, window_days=window_days, window=window, at=[q])
    return float(result.median[0]), float(result.mad[0]), float(result.n_window[0])


def test_the_fixtures_are_labelled_synthetic() -> None:
    assert SYNTHETIC is True


# --- hand-computed references -----------------------------------------------------------------


def test_trailing_window_by_hand() -> None:
    # q = 9, W = 5 -> [4, 9]: 12, 11, 13, 10, 12, 11 -> sorted 10 11 11 12 12 13, median 11.5;
    # deviations .5 .5 1.5 1.5 .5 .5 -> sorted .5 .5 .5 .5 1.5 1.5 -> MAD 0.5
    result = rolling_baseline(HAND, window_days=5, at=[9.0])
    assert result.median[0] == 11.5
    assert result.mad[0] == 0.5
    assert result.n_window == (6,)
    assert result.scale[0] == pytest.approx(0.5 * MAD_SCALE)
    assert result.low[0] == pytest.approx(11.5 - 2 * 0.5 * 1.4826, abs=1e-3)  # 10.0174
    assert result.high[0] == pytest.approx(11.5 + 2 * 0.5 * 1.4826, abs=1e-3)  # 12.9826
    assert result.valid == (True,)


def test_the_spike_does_not_move_the_baseline() -> None:
    # q = 5, W = 5 -> [0, 5]: 10 12 11 30 12 11 -> sorted 10 11 11 12 12 30, median 11.5;
    # deviations 1.5 .5 .5 18.5 .5 .5 -> MAD 0.5. The mean of the window would be 14.33.
    median, spread, count = at(HAND, 5.0, 5.0, Window.TRAILING)
    assert (median, spread, count) == (11.5, 0.5, 6.0)


def test_preceding_window_leaves_out_the_point_at_q() -> None:
    # q = 9, W = 5 -> [4, 9): 12 11 13 10 12 -> median 12; deviations 0 1 1 2 0 -> MAD 1
    assert at(HAND, 9.0, 5.0, Window.PRECEDING) == (12.0, 1.0, 5.0)


def test_centered_window_by_hand() -> None:
    # q = 5, W = 4 -> [3, 7]: 30 12 11 13 10 -> median 12; deviations 18 0 1 1 2 -> MAD 1
    assert at(HAND, 5.0, 4.0, Window.CENTERED) == (12.0, 1.0, 5.0)


@pytest.mark.parametrize(
    ("window", "q", "expected_count"),
    [
        (Window.TRAILING, 9.0, 6),  # [4, 9]: both ends included
        (Window.TRAILING, 9.5, 5),  # [4.5, 9.5]: 4 falls out
        (Window.TRAILING, 8.999, 5),  # [3.999, 8.999]: 3 is out, 4..8 in
        (Window.PRECEDING, 9.0, 5),  # [4, 9): 9 is out
        (Window.CENTERED, 5.0, 5),  # [3, 7] for W = 4
    ],
)
def test_window_boundaries_are_inclusive_where_documented(
    window: Window, q: float, expected_count: int
) -> None:
    width = 4.0 if window is Window.CENTERED else 5.0
    result = rolling_baseline(HAND, window_days=width, window=window, at=[q], min_points=1)
    assert result.n_window == (expected_count,)


def test_the_range_is_median_plus_minus_k_scales_and_k_is_a_parameter() -> None:
    wide = rolling_baseline(HAND, window_days=5, at=[9.0], k=3.0)
    assert wide.high[0] - wide.median[0] == pytest.approx(3 * 0.5 * MAD_SCALE)
    assert wide.median[0] - wide.low[0] == pytest.approx(3 * 0.5 * MAD_SCALE)


def test_min_scale_keeps_a_flat_window_from_having_a_zero_width_range() -> None:
    flat = Series.from_arrays(np.arange(6.0), [80.0] * 6)
    bare = rolling_baseline(flat, window_days=5, at=[5.0])
    floored = rolling_baseline(flat, window_days=5, at=[5.0], min_scale=0.1)
    assert (bare.mad[0], bare.low[0], bare.high[0]) == (0.0, 80.0, 80.0)
    assert floored.scale[0] == 0.1
    assert (floored.low[0], floored.high[0]) == pytest.approx((79.8, 80.2))


# --- the same answer from a plain reference implementation ------------------------------------


def reference(
    times: np.ndarray, values: np.ndarray, q: float, width: float, window: Window
) -> tuple[float, float, int] | None:
    """The textbook definition with explicit comparisons, no searchsorted."""
    if window is Window.TRAILING:
        chosen = [v for t, v in zip(times, values, strict=True) if q - width <= t <= q]
    elif window is Window.PRECEDING:
        chosen = [v for t, v in zip(times, values, strict=True) if q - width <= t < q]
    else:
        chosen = [
            v for t, v in zip(times, values, strict=True) if q - width / 2 <= t <= q + width / 2
        ]
    if len(chosen) < 3:
        return None
    centre = float(np.median(chosen))
    return centre, float(np.median([abs(v - centre) for v in chosen])), len(chosen)


@pytest.mark.parametrize("window", list(Window))
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_it_matches_the_reference_on_irregular_series_with_gaps(window: Window, seed: int) -> None:
    rng = generator(seed)
    times = irregular_times(rng, 120, 200.0, with_gaps=True)
    values = rng.normal(80.0, 1.5, size=times.size)
    series = Series.from_arrays(times, values)
    result = rolling_baseline(series, window_days=17.5, window=window)
    for i, q in enumerate(times):
        expected = reference(times, values, float(q), 17.5, window)
        if expected is None:
            assert not result.valid[i]
            assert math.isnan(result.median[i])
        else:
            assert result.valid[i]
            assert (float(result.median[i]), float(result.mad[i]), result.n_window[i]) == expected


# --- properties: the window is in time, not in points -----------------------------------------


def test_repeating_every_reading_changes_neither_the_median_nor_the_mad() -> None:
    rng = generator(11)
    times = irregular_times(rng, 80, 120.0)
    values = rng.normal(0.0, 1.0, size=times.size)
    once = rolling_baseline(Series.from_arrays(times, values), window_days=20.0, min_points=1)
    thrice = rolling_baseline(
        Series.from_arrays(np.repeat(times, 3), np.repeat(values, 3)),
        window_days=20.0,
        min_points=1,
        at=times,
    )
    np.testing.assert_array_equal(once.median, thrice.median)
    np.testing.assert_array_equal(once.mad, thrice.mad)
    assert thrice.n_window[0] == 3 * once.n_window[0]


def test_sparse_and_dense_sampling_of_one_signal_give_the_same_baseline() -> None:
    # a level of 80 until day 50, then 85; the 20-day window at day 100 sees only the 85 level
    def signal(t: np.ndarray) -> np.ndarray:
        return np.where(t < 50.0, 80.0, 85.0)

    sparse = np.arange(0.0, 101.0, 5.0)
    dense = np.arange(0.0, 101.0, 0.25)
    results = [
        rolling_baseline(Series.from_arrays(t, signal(t)), window_days=20.0, at=[100.0])
        for t in (sparse, dense)
    ]
    assert results[0].n_window[0] < results[1].n_window[0]  # not the same number of points ...
    for r in results:  # ... the same baseline
        assert (r.median[0], r.mad[0], r.low[0], r.high[0]) == (85.0, 0.0, 85.0, 85.0)


def test_shifting_the_times_does_not_change_the_baseline() -> None:
    rng = generator(5)
    times = irregular_times(rng, 90, 150.0)
    values = rng.normal(size=times.size)
    base = rolling_baseline(Series.from_arrays(times, values), window_days=21.0)
    moved = rolling_baseline(Series.from_arrays(times + 20_000.0, values), window_days=21.0)
    np.testing.assert_array_equal(base.median, moved.median)
    np.testing.assert_array_equal(base.mad, moved.mad)
    assert base.n_window == moved.n_window


def test_the_time_unit_does_not_matter_when_the_window_is_converted_with_it() -> None:
    rng = generator(6)
    times = np.round(irregular_times(rng, 90, 150.0))  # whole days, exact when scaled
    times = np.unique(times)
    values = rng.normal(size=times.size)
    days = rolling_baseline(Series.from_arrays(times, values), window_days=21.0)
    hours = rolling_baseline(Series.from_arrays(times * 24.0, values), window_days=21.0 * 24.0)
    np.testing.assert_array_equal(days.median, hours.median)
    assert days.n_window == hours.n_window


def test_a_scale_and_offset_of_the_values_carry_through() -> None:
    rng = generator(8)
    times = irregular_times(rng, 70, 100.0)
    values = rng.normal(size=times.size)
    plain = rolling_baseline(Series.from_arrays(times, values), window_days=15.0)
    changed = rolling_baseline(Series.from_arrays(times, 3.0 * values + 50.0), window_days=15.0)
    np.testing.assert_allclose(changed.median, 3.0 * plain.median + 50.0, equal_nan=True)
    np.testing.assert_allclose(changed.mad, 3.0 * plain.mad, equal_nan=True)


def test_the_input_order_does_not_matter() -> None:
    rng = generator(9)
    times = irregular_times(rng, 60, 90.0)
    values = rng.normal(size=times.size)
    order = rng.permutation(times.size)
    a = rolling_baseline(Series.from_arrays(times, values), window_days=14.0)
    b = rolling_baseline(Series.from_arrays(times[order], values[order]), window_days=14.0)
    np.testing.assert_array_equal(a.median, b.median)


# --- irregular sampling and gaps ---------------------------------------------------------------


def test_a_gap_longer_than_the_window_has_no_baseline_and_nothing_is_invented() -> None:
    times = np.concatenate([np.arange(0.0, 10.0), np.arange(60.0, 70.0)])
    series = Series.from_arrays(times, np.arange(20.0))
    result = rolling_baseline(series, window_days=14.0, at=[5.0, 30.0, 45.0, 65.0])
    assert result.valid == (True, False, False, True)
    assert result.n_window[1] == result.n_window[2] == 0
    assert np.isnan(result.median[[1, 2]]).all()
    assert np.isnan(result.low[[1, 2]]).all()


def test_a_constant_signal_sampled_irregularly_has_the_exact_level_wherever_valid() -> None:
    rng = generator(21)
    times = irregular_times(rng, 100, 365.0, with_gaps=True)  # nothing between day 146 and 219
    queries = np.append(times, 190.0)  # one query in the middle of the hole
    result = rolling_baseline(
        Series.from_arrays(times, np.full(times.size, 82.4)), window_days=30.0, at=queries
    )
    valid = np.array(result.valid)
    assert not valid[-1]
    assert valid[:-1].sum() > 50
    assert (result.median[valid] == 82.4).all()
    assert (result.mad[valid] == 0.0).all()
    assert np.isnan(result.median[~valid]).all()


def test_too_few_points_is_not_valid_and_min_points_is_inclusive() -> None:
    series = Series.from_arrays([0.0, 1.0, 2.0], [1.0, 2.0, 3.0])
    assert rolling_baseline(series, window_days=10.0, at=[2.0], min_points=3).valid == (True,)
    assert rolling_baseline(series, window_days=10.0, at=[2.0], min_points=4).valid == (False,)


def test_an_empty_series_and_no_queries_do_not_crash() -> None:
    empty = Series.from_arrays([], [])
    assert rolling_baseline(empty, window_days=7.0).valid == ()
    result = rolling_baseline(empty, window_days=7.0, at=[1.0, 2.0])
    assert result.valid == (False, False)
    assert result.inputs.n == 0


def test_queries_may_lie_before_after_or_between_the_points_and_in_any_order() -> None:
    series = Series.from_arrays(np.arange(0.0, 10.0), np.arange(10.0))
    result = rolling_baseline(series, window_days=4.0, at=[20.0, -5.0, 4.5], min_points=1)
    assert result.valid == (False, False, True)
    # [0.5, 4.5] holds t = 1, 2, 3, 4 -> values 1 2 3 4 -> median 2.5
    assert result.median[2] == 2.5
    assert result.n_window[2] == 4


# --- result and arguments ----------------------------------------------------------------------


def test_the_result_carries_n_coverage_and_the_algorithm_version() -> None:
    result = rolling_baseline(HAND, window_days=5)
    assert result.algo_version == ALGO_VERSION == "baseline/1"
    assert result.inputs.n == 10
    assert float(result.inputs.coverage) == 1.0
    decoded = json.loads(json.dumps(rolling_baseline(HAND, window_days=5, at=[1.0]).as_dict()))
    assert decoded["median"] == [None]  # NaN is not JSON
    assert decoded["algo_version"] == "baseline/1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_days": 0},
        {"window_days": -1},
        {"window_days": 5, "min_points": 0},
        {"window_days": 5, "k": 0},
        {"window_days": 5, "min_scale": -1},
        {"window_days": 5, "at": [float("nan")]},
        {"window_days": 5, "at": [[1.0, 2.0]]},
    ],
)
def test_bad_arguments_are_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"must|finite"):
        rolling_baseline(HAND, **kwargs)  # type: ignore[arg-type]
