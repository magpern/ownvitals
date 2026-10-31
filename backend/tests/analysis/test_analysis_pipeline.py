"""The whole pipeline (quality gates, daily values, data quality, policy) on seeded synthetic
series with gaps, clustered repeats, travel, DST days, attrib=1 readings, jumps, implausible
readings and re-delivered duplicates. SYNTHETIC data only."""

import random
from collections import Counter
from datetime import date, timedelta

import pytest
from synthetic_readings import (
    DAYS,
    DST_DAYS,
    HOLE,
    START,
    SYNTHETIC,
    TRIPS,
    SyntheticSeries,
    make_series,
)

from ownvitals.analysis.daily import DailyMethod, DailyValue, daily_values, is_morning
from ownvitals.analysis.policy import InsufficientData, Purpose, Sufficient, evaluate_metric
from ownvitals.analysis.quality import UNCONFIRMED_ATTRIB, Flag, Reading, assess

SEEDS = [1, 2, 3, 4, 5, 20261031]
END = START + timedelta(days=DAYS - 1)  # the last day of the default series


def test_the_generator_is_labelled_synthetic() -> None:
    assert SYNTHETIC is True


# --- the generator ----------------------------------------------------------------------------


def test_the_generator_is_deterministic_and_the_seed_matters() -> None:
    assert make_series(7) == make_series(7)
    assert make_series(7).readings != make_series(8).readings


@pytest.mark.parametrize("seed", SEEDS)
def test_the_generator_plants_every_kind_of_irregularity(seed: int) -> None:
    series = make_series(seed)
    readings = series.readings
    zones = {r.source_tz for r in readings}
    assert {"Europe/Stockholm", "America/New_York", "Asia/Tokyo"} <= zones  # travel
    assert any(r.attrib == UNCONFIRMED_ATTRIB for r in readings)
    assert len({r.grpid for r in readings}) < len(readings)  # re-delivered duplicates
    truth = series.truth
    assert len(truth.jumps) == 6
    assert len(truth.implausible) == 3
    assert len(truth.duplicated) == 5
    assert truth.household
    by_day = Counter(r.local_date for r in readings if r.attrib != UNCONFIRMED_ATTRIB)
    assert {d for d in DST_DAYS if by_day[d] >= 2} == set(DST_DAYS)  # readings on both DST days
    assert any(n >= 2 for n in by_day.values())  # clustered repeats and evening readings
    first, last = HOLE
    assert not [d for d in by_day if first <= d <= last]  # the three-week hole
    assert any(  # a trip's readings carry the trip's zone only on the trip's days
        r.source_tz == zone and first <= r.local_date <= last
        for first, last, zone in TRIPS
        for r in readings
    )


# --- quality gates -----------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_every_planted_irregularity_is_flagged_and_nothing_is_dropped(seed: int) -> None:
    series = make_series(seed)
    result = assess("weight", series.readings)
    assert len(result.readings) == len(series.readings)  # kept, flagged, not used
    for assessed in result.readings:
        assert (Flag.UNCONFIRMED in assessed.flags) == (assessed.reading.attrib == 1)
    assert {r.grpid for r in result.flagged(Flag.IMPLAUSIBLE)} == series.truth.implausible
    duplicates = [r.grpid for r in result.flagged(Flag.DUPLICATE)]
    assert sorted(duplicates) == sorted(series.truth.duplicated)
    jumped = {a.reading.grpid for a in result.readings if Flag.JUMP in a.flags}
    assert series.truth.jumps <= jumped


@pytest.mark.parametrize("seed", SEEDS)
def test_a_jump_flag_without_a_planted_jump_only_follows_one_within_24_hours(seed: int) -> None:
    series = make_series(seed)
    result = assess("weight", series.readings)
    planted = [r.measured_at for r in series.readings if r.grpid in series.truth.jumps]
    for reading in result.flagged(Flag.JUMP):
        if reading.grpid in series.truth.jumps:
            continue
        assert any(timedelta(0) < reading.measured_at - t <= timedelta(hours=24) for t in planted)


@pytest.mark.parametrize("seed", SEEDS)
def test_usable_readings_are_confirmed_plausible_and_unique(seed: int) -> None:
    usable = assess("weight", make_series(seed).readings).usable
    assert all(r.attrib != UNCONFIRMED_ATTRIB for r in usable)
    assert all(r.value >= 10 and r.value <= 300 for r in usable)
    assert len({r.grpid for r in usable}) == len(usable)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_result_is_independent_of_input_order(seed: int) -> None:
    series = make_series(seed)
    shuffled = list(series.readings)
    random.Random(seed).shuffle(shuffled)  # noqa: S311 - seeded for reproducible tests, not for security
    assert assess("weight", shuffled) == assess("weight", series.readings)


# --- daily values ------------------------------------------------------------------------------


def values_of(series: SyntheticSeries) -> tuple[tuple[Reading, ...], tuple[DailyValue, ...]]:
    usable = assess("weight", series.readings).usable
    return usable, daily_values(usable)


@pytest.mark.parametrize("seed", SEEDS)
def test_daily_values_account_for_every_usable_reading(seed: int) -> None:
    usable, values = values_of(make_series(seed))
    assert sum(v.n for v in values) == len(usable)
    assert [v.day for v in values] == sorted({r.local_date for r in usable})
    for v in values:
        assert v.minimum <= v.value <= v.maximum
        assert v.mad >= 0
    assert not [v for v in values if HOLE[0] <= v.day <= HOLE[1]]  # nothing invented in the hole


@pytest.mark.parametrize("seed", SEEDS)
def test_daily_method_is_morning_exactly_when_the_day_has_a_morning_reading(seed: int) -> None:
    usable, values = values_of(make_series(seed))
    morning_days = {r.local_date for r in usable if is_morning(r)}
    for v in values:
        assert (v.method is DailyMethod.MORNING) == (v.day in morning_days)
    assert Counter(v.method for v in values)[DailyMethod.MORNING] > 100


@pytest.mark.parametrize("seed", SEEDS)
def test_no_household_or_implausible_weight_reaches_a_daily_value(seed: int) -> None:
    _, values = values_of(make_series(seed))
    # household members weigh about 55 kg and implausible readings are 0.8 or 650 kg
    assert all(v.minimum > 70 and v.maximum < 100 for v in values)


@pytest.mark.parametrize("seed", SEEDS)
def test_dst_days_and_trips_have_their_own_local_days(seed: int) -> None:
    usable, values = values_of(make_series(seed))
    days = {v.day for v in values}
    assert set(DST_DAYS) <= days
    for first, last, zone in TRIPS:
        abroad = [r for r in usable if r.source_tz == zone]
        assert abroad
        assert all(first <= r.local_date <= last for r in abroad)


# --- minimum-data policy on the series --------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_a_dense_stretch_passes_trend_and_baseline(seed: int) -> None:
    _, values = values_of(make_series(seed))
    for purpose in Purpose:
        result = evaluate_metric("weight", purpose, values, window_end=END, window_days=60)
        assert isinstance(result, Sufficient), (purpose, result)
        assert result.data_quality.n_days >= 30


@pytest.mark.parametrize("seed", SEEDS)
def test_the_three_week_hole_gives_insufficient_data_not_a_value(seed: int) -> None:
    _, values = values_of(make_series(seed))
    result = evaluate_metric(
        "weight",
        Purpose.TREND,
        values,
        window_end=HOLE[1],
        window_days=(HOLE[1] - HOLE[0]).days + 1,
    )
    assert isinstance(result, InsufficientData)
    assert result.n == 0
    assert result.coverage == 0
    assert result.data_quality.longest_gap_days == (HOLE[1] - HOLE[0]).days + 1


@pytest.mark.parametrize("seed", SEEDS)
def test_a_sparse_series_is_insufficient_for_both_purposes(seed: int) -> None:
    series = make_series(seed, days=60, miss_probability=0.93, start=date(2026, 8, 1))
    _, values = values_of(series)
    assert 0 < len(values) < 8
    for purpose in Purpose:
        result = evaluate_metric(
            "weight", purpose, values, window_end=date(2026, 9, 29), window_days=60
        )
        assert isinstance(result, InsufficientData)
        assert result.n == len(values)
        assert result.shortfalls
