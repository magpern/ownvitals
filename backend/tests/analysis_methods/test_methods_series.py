"""``Series``: validation, canonical order, the daily/instant constructors and ``InputSummary``.
SYNTHETIC data only."""

import json
from datetime import UTC, date, datetime, timedelta, timezone
from fractions import Fraction

import numpy as np
import pytest
from methods_builders import SYNTHETIC, daily_values

from ownvitals.analysis.methods.series import MAD_SCALE, Series, mad


def test_the_fixtures_are_labelled_synthetic() -> None:
    assert SYNTHETIC is True


def test_the_arrays_are_copied_and_read_only() -> None:
    t = np.array([0.0, 1.0, 2.0])
    y = np.array([5.0, 6.0, 7.0])
    series = Series(t, y)
    t[0] = 99.0
    assert series.t[0] == 0.0
    with pytest.raises(ValueError, match="read-only"):
        series.y[0] = 1.0


@pytest.mark.parametrize(
    ("t", "y"),
    [
        ([0.0, 1.0], [1.0]),  # lengths differ
        ([0.0, float("nan")], [1.0, 2.0]),
        ([0.0, 1.0], [1.0, float("inf")]),
        ([1.0, 0.0], [1.0, 2.0]),  # not sorted
    ],
)
def test_invalid_input_is_refused(t: list[float], y: list[float]) -> None:
    with pytest.raises(ValueError, match=r"same length|finite|non-decreasing"):
        Series(np.array(t), np.array(y))


def test_a_two_dimensional_input_is_refused() -> None:
    with pytest.raises(ValueError, match="one-dimensional"):
        Series(np.zeros((2, 2)), np.zeros((2, 2)))
    with pytest.raises(ValueError, match="one-dimensional"):
        Series.from_arrays(np.zeros((2, 2)), np.zeros((2, 2)))


def test_from_arrays_sorts_by_time_then_value_whatever_the_input_order() -> None:
    a = Series.from_arrays([2.0, 0.0, 1.0, 1.0], [7.0, 5.0, 9.0, 6.0])
    b = Series.from_arrays([1.0, 1.0, 2.0, 0.0], [6.0, 9.0, 7.0, 5.0])
    assert a.t.tolist() == b.t.tolist() == [0.0, 1.0, 1.0, 2.0]
    assert a.y.tolist() == b.y.tolist() == [5.0, 6.0, 9.0, 7.0]


def test_the_empty_series_is_allowed_and_has_no_coverage() -> None:
    summary = Series.from_arrays([], []).summary()
    assert (summary.n, summary.n_days, summary.window_days) == (0, 0, 0)
    assert summary.coverage == Fraction(0)
    assert summary.first is None
    assert summary.last is None


def test_summary_counts_days_and_measures_coverage_over_the_span_by_default() -> None:
    # two readings on day 0, one on day 2, one on day 4: 3 distinct days in a span of 5
    series = Series.from_arrays([0.1, 0.9, 2.5, 4.0], [1.0, 2.0, 3.0, 4.0])
    summary = series.summary()
    assert (summary.n, summary.n_days, summary.window_days) == (4, 3, 5)
    assert summary.coverage == Fraction(3, 5)
    assert (summary.first, summary.last) == (0.1, 4.0)


def test_summary_uses_the_declared_window() -> None:
    series = Series.from_arrays([0.0, 10.0, 20.0], [1.0, 2.0, 3.0], window_days=90)
    assert series.summary().coverage == Fraction(3, 90)
    assert series.summary().window_days == 90


def test_a_window_shorter_than_the_days_with_data_is_refused() -> None:
    with pytest.raises(ValueError, match="shorter"):
        Series.from_arrays([0.0, 1.0, 2.0], [1.0, 2.0, 3.0], window_days=2)
    with pytest.raises(ValueError, match="at least 1"):
        Series.from_arrays([0.0], [1.0], window_days=0)


def test_from_daily_uses_days_since_1970_and_converts_decimals_to_floats() -> None:
    daily = daily_values([0, 2, 3], [80.5, 80.25, 81.0])
    series = Series.from_daily(daily, window_days=7)
    first_day = (daily[0].day - date(1970, 1, 1)).days
    assert series.t.tolist() == [first_day, first_day + 2, first_day + 3]
    assert series.y.tolist() == [80.5, 80.25, 81.0]
    assert series.summary().n_days == 3
    assert series.summary().coverage == Fraction(3, 7)


def test_from_instants_converts_to_days_and_refuses_naive_times() -> None:
    noon = datetime(1970, 1, 3, 12, tzinfo=UTC)
    # the same instant written in another zone
    same = datetime(1970, 1, 3, 14, tzinfo=timezone(timedelta(hours=2)))
    series = Series.from_instants([same, noon + timedelta(days=1)], [1.0, 2.0])
    assert series.t.tolist() == [2.5, 3.5]
    with pytest.raises(ValueError, match="timezone-aware"):
        Series.from_instants([datetime(2026, 1, 1)], [1.0])


def test_mad_is_the_raw_median_absolute_deviation() -> None:
    # median 3; deviations 2, 1, 0, 1, 97 -> sorted 0, 1, 1, 2, 97 -> median 1
    assert mad(np.array([1.0, 2.0, 3.0, 4.0, 100.0])) == 1.0
    assert pytest.approx(1.4826, abs=1e-4) == MAD_SCALE


def test_the_summary_is_json_safe() -> None:
    series = Series.from_arrays([0.0, 1.5, 4.0], [1.0, 2.0, 3.0], window_days=10)
    decoded = json.loads(json.dumps(series.summary().as_dict()))
    assert decoded["n"] == 3
    assert decoded["coverage"] == pytest.approx(0.3)
