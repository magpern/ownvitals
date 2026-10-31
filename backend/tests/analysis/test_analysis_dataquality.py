import json
import re
from dataclasses import replace
from datetime import date
from fractions import Fraction

import pytest
from analysis_builders import daily_on

from ownvitals.analysis import dataquality
from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.dataquality import Gap, summarise

END = date(2026, 6, 30)  # a 30-day window is 1 to 30 June


def jun(day: int) -> date:
    return date(2026, 6, day)


def with_counts(counts: dict[int, int]) -> list[DailyValue]:
    return [replace(daily_on(jun(day))[0], n=n) for day, n in counts.items()]


# One example used by several tests: data on 3, 4, 5, 10, 11 and 20 June, with 9 readings.
EXAMPLE = with_counts({3: 1, 4: 2, 5: 1, 10: 1, 11: 3, 20: 1})


def test_counts_coverage_and_span() -> None:
    q = summarise(EXAMPLE, window_end=END, window_days=30)
    assert (q.window_start, q.window_end, q.window_days) == (jun(1), jun(30), 30)
    assert (q.n_readings, q.n_days) == (9, 6)
    assert (q.first_day, q.last_day, q.span_days) == (jun(3), jun(20), 17)  # days elapsed
    assert q.coverage == Fraction(6, 30)
    assert q.span_fraction == Fraction(17, 30)


@pytest.mark.parametrize(("last_day", "span"), [(28, 27), (29, 28), (30, 29)])
def test_the_span_is_the_days_elapsed_between_the_first_and_last_day(
    last_day: int, span: int
) -> None:
    q = summarise(daily_on(jun(1), jun(last_day)), window_end=END, window_days=30)
    assert q.span_days == span == (jun(last_day) - jun(1)).days
    assert q.span_fraction == Fraction(span, 30)


def test_data_on_the_1st_and_the_29th_is_a_span_of_exactly_four_weeks() -> None:
    q = summarise(daily_on(jun(1), jun(29)), window_end=END, window_days=30)
    assert q.span_days == 28
    assert (jun(29) - jun(1)).days == 4 * 7
    assert q.n_days == 2  # a span is not a count of days: only two days have data


def test_gaps_are_runs_of_missing_days_including_the_edges() -> None:
    q = summarise(EXAMPLE, window_end=END, window_days=30)
    assert q.min_gap_days == 3
    assert q.gaps == (Gap(jun(6), jun(9)), Gap(jun(12), jun(19)), Gap(jun(21), jun(30)))
    assert [g.days for g in q.gaps] == [4, 8, 10]
    assert q.longest_gap_days == 10


def test_a_gap_is_listed_from_min_gap_days_but_the_longest_counts_every_run() -> None:
    narrow = summarise(EXAMPLE, window_end=END, window_days=30, min_gap_days=2)
    assert narrow.gaps[0] == Gap(jun(1), jun(2))  # the 2-day run before 3 June
    wide = summarise(EXAMPLE, window_end=END, window_days=30, min_gap_days=11)
    assert wide.gaps == ()
    assert wide.longest_gap_days == 10


@pytest.mark.parametrize(("min_gap_days", "listed"), [(1, True), (2, True), (3, False)])
def test_gap_threshold_is_inclusive(min_gap_days: int, listed: bool) -> None:
    daily = daily_on(jun(1), jun(4))  # two missing days, 2 and 3 June
    q = summarise(daily, window_end=jun(4), window_days=4, min_gap_days=min_gap_days)
    assert (Gap(jun(2), jun(3)) in q.gaps) is listed
    assert q.longest_gap_days == 2


def test_measurement_frequency() -> None:
    q = summarise(EXAMPLE, window_end=END, window_days=30)
    assert q.n_readings == 9
    assert q.readings_per_week == Fraction(7 * 9, 30)
    # distances between the days with data: 1, 1, 5, 1, 9
    assert q.median_days_between == 1


def test_median_distance_between_days_of_an_even_count_is_exact() -> None:
    q = summarise(daily_on(jun(1), jun(2), jun(4)), window_end=jun(4), window_days=4)
    assert q.median_days_between == Fraction(3, 2)


def test_days_outside_the_window_are_ignored() -> None:
    inside = daily_on(jun(10), jun(12))
    outside = daily_on(jun(9), jun(13), date(2026, 5, 31), date(2026, 7, 1))
    q = summarise([*outside, *inside], window_end=jun(12), window_days=3)  # 10 to 12 June
    assert (q.window_start, q.n_days, q.n_readings, q.span_days) == (jun(10), 2, 2, 2)
    assert q.gaps == ()
    assert q.longest_gap_days == 1


def test_the_window_edges_are_inclusive() -> None:
    q = summarise(daily_on(jun(1), jun(30)), window_end=END, window_days=30)
    assert (q.n_days, q.coverage) == (2, Fraction(1, 15))
    # first to last day of a 30-day window: 29 days elapsed, so the fraction stays below 1
    assert (q.span_days, q.span_fraction) == (29, Fraction(29, 30))
    assert q.gaps == (Gap(jun(2), jun(29)),)


def test_no_data_is_one_gap_the_size_of_the_window() -> None:
    q = summarise([], window_end=END, window_days=30)
    assert (q.n_days, q.n_readings, q.span_days, q.coverage) == (0, 0, 0, 0)
    assert (q.first_day, q.last_day, q.median_days_between) == (None, None, None)
    assert q.gaps == (Gap(jun(1), jun(30)),)
    assert q.longest_gap_days == 30
    assert q.readings_per_week == 0


def test_a_single_day_has_a_span_of_zero_and_no_interval() -> None:
    q = summarise(daily_on(jun(7)), window_end=END, window_days=30)
    assert (q.n_days, q.span_days, q.span_fraction, q.median_days_between) == (1, 0, 0, None)


def test_no_gaps_when_every_day_has_data() -> None:
    days = [jun(d) for d in range(1, 8)]
    q = summarise(daily_on(*days), window_end=jun(7), window_days=7)
    assert (q.gaps, q.longest_gap_days, q.coverage) == ((), 0, 1)
    assert (q.span_days, q.span_fraction) == (6, Fraction(6, 7))


def test_two_daily_values_for_one_day_are_refused() -> None:
    with pytest.raises(ValueError, match="2026-06-07"):
        summarise(daily_on(jun(7), jun(7)), window_end=END, window_days=30)


@pytest.mark.parametrize(("window_days", "min_gap_days"), [(0, 3), (-1, 3), (30, 0)])
def test_invalid_parameters_are_refused(window_days: int, min_gap_days: int) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        summarise([], window_end=END, window_days=window_days, min_gap_days=min_gap_days)


def test_as_dict_is_json_safe_and_complete() -> None:
    q = summarise(EXAMPLE, window_end=END, window_days=30)
    as_dict = q.as_dict()
    assert json.loads(json.dumps(as_dict)) == as_dict
    assert as_dict["window_start"] == "2026-06-01"
    assert as_dict["gaps"][0] == {"start": "2026-06-06", "end": "2026-06-09", "days": 4}
    assert as_dict["coverage"] == 0.2
    assert as_dict["span_days"] == 17
    assert as_dict["span_fraction"] == 0.5667
    assert as_dict["algo_version"] == dataquality.ALGO_VERSION
    empty = summarise([], window_end=END, window_days=30).as_dict()
    assert empty["first_day"] is None
    assert empty["median_days_between"] is None
    assert json.loads(json.dumps(empty)) == empty


def test_the_result_is_deterministic_and_independent_of_input_order() -> None:
    days = [jun(d) for d in (20, 3, 11, 5, 4, 10)]
    a = summarise(daily_on(*days), window_end=END, window_days=30)
    b = summarise(daily_on(*sorted(days)), window_end=END, window_days=30)
    assert a == b


def test_algo_version_format() -> None:
    assert re.fullmatch(r"dataquality/\d+", dataquality.ALGO_VERSION)
