import random
import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from analysis_builders import STOCKHOLM, reading, utc

from ownvitals.analysis import daily
from ownvitals.analysis.daily import DailyMethod, DailyValue, daily_values
from ownvitals.analysis.quality import Reading

D = Decimal


def local(day: date, at: time, tz: str = STOCKHOLM, fold: int = 0) -> datetime:
    """The instant of a wall-clock time in ``tz`` (DST resolved by the zone rules)."""
    return datetime.combine(day, at, tzinfo=ZoneInfo(tz)).replace(fold=fold).astimezone(UTC)


def one_day(*readings: Reading) -> DailyValue:
    (value,) = daily_values(readings)
    return value


JUNE_15 = date(2026, 6, 15)  # Stockholm is on UTC+2 (CEST)


# --- the morning window -----------------------------------------------------------------------


def test_morning_window_the_first_reading_between_0400_and_1100_wins() -> None:
    value = one_day(
        reading(local(JUNE_15, time(7, 0)), "80.0"),
        reading(local(JUNE_15, time(6, 30)), "80.4"),
        reading(local(JUNE_15, time(19, 0)), "81.0"),
    )
    assert (value.method, value.value, value.day) == (DailyMethod.MORNING, D("80.4"), JUNE_15)
    assert (value.n, value.minimum, value.maximum) == (3, D("80.0"), D("81.0"))


def test_morning_window_skips_an_earlier_reading_outside_the_window() -> None:
    value = one_day(
        reading(local(JUNE_15, time(2, 0)), "79.0"),  # before 04:00
        reading(local(JUNE_15, time(8, 0)), "80.0"),
    )
    assert (value.method, value.value, value.n) == (DailyMethod.MORNING, D("80.0"), 2)


@pytest.mark.parametrize(
    ("at", "method"),
    [
        (time(3, 59, 59, 999999), DailyMethod.MEDIAN),
        (time(4, 0), DailyMethod.MORNING),  # 04:00 is inside
        (time(4, 0, 0, 1), DailyMethod.MORNING),
        (time(10, 59, 59, 999999), DailyMethod.MORNING),
        (time(11, 0), DailyMethod.MEDIAN),  # 11:00 is outside
    ],
)
def test_morning_window_edges_are_04_00_inclusive_and_11_00_exclusive(
    at: time, method: DailyMethod
) -> None:
    value = one_day(
        reading(local(JUNE_15, at), "80.0"),
        reading(local(JUNE_15, time(20, 0)), "82.0"),
        reading(local(JUNE_15, time(21, 0)), "83.0"),
    )
    assert value.method is method
    assert value.value == (D("80.0") if method is DailyMethod.MORNING else D("82.0"))


def test_morning_window_is_decided_on_the_instant_not_the_input_order() -> None:
    a = reading(local(JUNE_15, time(6, 0)), "80.0")
    b = reading(local(JUNE_15, time(9, 0)), "81.0")
    assert one_day(b, a).value == one_day(a, b).value == D("80.0")


# --- the median fallback ----------------------------------------------------------------------


def test_median_fallback_when_no_reading_is_in_the_morning_window() -> None:
    value = one_day(
        reading(local(JUNE_15, time(12, 0)), "82.0"),
        reading(local(JUNE_15, time(18, 0)), "80.0"),
        reading(local(JUNE_15, time(22, 0)), "81.0"),
    )
    assert (value.method, value.value, value.n) == (DailyMethod.MEDIAN, D("81.0"), 3)


def test_median_of_an_even_count_is_the_exact_mean_of_the_middle_two() -> None:
    value = one_day(
        reading(local(JUNE_15, time(12, 0)), "80.1"),
        reading(local(JUNE_15, time(13, 0)), "80.2"),
    )
    assert value.value == D("80.15")
    assert value.method is DailyMethod.MEDIAN


def test_a_single_reading_is_its_own_daily_value_with_zero_spread() -> None:
    value = one_day(reading(local(JUNE_15, time(15, 0)), "79.9"))
    assert (value.value, value.n, value.minimum, value.maximum, value.mad) == (
        D("79.9"),
        1,
        D("79.9"),
        D("79.9"),
        D(0),
    )


def test_spread_covers_all_readings_of_the_day_not_only_the_morning_ones() -> None:
    value = one_day(
        reading(local(JUNE_15, time(6, 0)), "80.0"),
        reading(local(JUNE_15, time(7, 0)), "80.2"),
        reading(local(JUNE_15, time(8, 0)), "80.4"),
        reading(local(JUNE_15, time(20, 0)), "81.0"),
    )
    assert value.value == D("80.0")  # the first morning reading
    assert (value.n, value.minimum, value.maximum) == (4, D("80.0"), D("81.0"))
    # median 80.3; absolute deviations 0.3, 0.1, 0.1, 0.7; their median is (0.1 + 0.3) / 2
    assert value.mad == D("0.2")


# --- days and order ---------------------------------------------------------------------------


def test_one_value_per_local_day_ascending_and_none_for_empty_days() -> None:
    values = daily_values(
        [
            reading(local(date(2026, 6, 17), time(7, 0))),
            reading(local(date(2026, 6, 15), time(7, 0))),
            reading(local(date(2026, 6, 15), time(21, 0))),
        ]
    )
    assert [(v.day, v.n) for v in values] == [(date(2026, 6, 15), 2), (date(2026, 6, 17), 1)]
    assert daily_values([]) == ()


def test_the_same_grpid_twice_is_refused() -> None:
    a = reading(local(JUNE_15, time(7, 0)), grpid=1)
    with pytest.raises(ValueError, match="grpid 1"):
        daily_values([a, a])


def test_the_result_carries_the_algorithm_version() -> None:
    assert one_day(reading(local(JUNE_15, time(7, 0)))).algo_version == daily.ALGO_VERSION
    assert re.fullmatch(r"daily/\d+", daily.ALGO_VERSION)


# --- DST --------------------------------------------------------------------------------------

SPRING = date(2026, 3, 29)  # Stockholm 01:00 UTC: 02:00 CET becomes 03:00 CEST (a 23 h day)
AUTUMN = date(2026, 10, 25)  # Stockholm 01:00 UTC: 03:00 CEST becomes 02:00 CET (a 25 h day)


def test_dst_spring_forward_the_morning_window_follows_the_local_clock() -> None:
    before = reading(utc(2026, 3, 29, 1, 59, 59), "79.0")  # 03:59:59 CEST: not morning
    at_four = reading(utc(2026, 3, 29, 2, 0, 0), "80.0")  # 04:00:00 CEST: morning (03:00 on UTC+1)
    value = one_day(before, at_four, reading(utc(2026, 3, 29, 17, 0), "82.0"))
    assert (value.method, value.value, value.day) == (DailyMethod.MORNING, D("80.0"), SPRING)


def test_dst_spring_forward_day_has_23_hours_and_keeps_all_its_readings() -> None:
    value = one_day(
        reading(utc(2026, 3, 28, 23, 0), "80.0"),  # 00:00 CET, the first moment of the day
        reading(utc(2026, 3, 29, 21, 59, 59), "80.2"),  # 23:59:59 CEST, the last
    )
    assert (value.day, value.n) == (SPRING, 2)
    assert daily_values([reading(utc(2026, 3, 29, 22, 0))])[0].day == date(2026, 3, 30)
    assert daily_values([reading(utc(2026, 3, 28, 22, 59, 59))])[0].day == date(2026, 3, 28)


def test_dst_fall_back_the_morning_window_follows_the_local_clock() -> None:
    before = reading(utc(2026, 10, 25, 2, 59, 59), "79.0")  # 03:59:59 CET: not morning
    at_four = reading(utc(2026, 10, 25, 3, 0, 0), "80.0")  # 04:00:00 CET: morning (05:00 on UTC+2)
    value = one_day(before, at_four, reading(utc(2026, 10, 25, 17, 0), "82.0"))
    assert (value.method, value.value, value.day) == (DailyMethod.MORNING, D("80.0"), AUTUMN)


def test_dst_fall_back_the_repeated_02_30_stays_inside_one_day() -> None:
    first = reading(local(AUTUMN, time(2, 30), fold=0), "80.0")  # 02:30 CEST
    second = reading(local(AUTUMN, time(2, 30), fold=1), "80.4")  # 02:30 CET, an hour later
    assert second.measured_at - first.measured_at == timedelta(hours=1)
    value = one_day(first, second)
    assert (value.day, value.n, value.method) == (AUTUMN, 2, DailyMethod.MEDIAN)
    assert value.value == D("80.2")


def test_dst_fall_back_day_has_25_hours_and_keeps_all_its_readings() -> None:
    value = one_day(
        reading(utc(2026, 10, 24, 22, 0), "80.0"),  # 00:00 CEST, the first moment of the day
        reading(utc(2026, 10, 25, 22, 59, 59), "80.2"),  # 23:59:59 CET, the last
    )
    assert (value.day, value.n) == (AUTUMN, 2)
    assert daily_values([reading(utc(2026, 10, 25, 23, 0))])[0].day == date(2026, 10, 26)


def test_dst_change_of_half_an_hour_lord_howe() -> None:
    zone = "Australia/Lord_Howe"  # 2026-04-05 02:00 (+11:00) becomes 01:30 (+10:30)
    start = date(2026, 4, 5)
    before = reading(utc(2026, 4, 4, 17, 29, 59), "79.0", tz=zone)  # 03:59:59: not morning
    inside = reading(utc(2026, 4, 4, 17, 30, 0), "80.0", tz=zone)  # 04:00:00: morning
    value = one_day(before, inside)
    assert (value.day, value.method, value.value) == (start, DailyMethod.MORNING, D("80.0"))


# --- crossing midnight ------------------------------------------------------------------------


def test_cross_midnight_a_reading_belongs_to_its_local_day_not_its_utc_day() -> None:
    late_utc = reading(utc(2026, 6, 14, 22, 30), "80.0")  # 00:30 on 15 June in Stockholm
    value = one_day(late_utc)
    assert (value.day, value.method) == (JUNE_15, DailyMethod.MEDIAN)  # 00:30 is not morning
    evening = reading(utc(2026, 6, 15, 21, 59, 59, 999999), "80.2")  # 23:59:59.999999 on the 15th
    assert one_day(late_utc, evening).n == 2
    assert daily_values([reading(utc(2026, 6, 15, 22, 0))])[0].day == date(2026, 6, 16)


@pytest.mark.parametrize(
    "tz",
    [
        "Europe/Stockholm",
        "America/New_York",
        "Asia/Kolkata",
        "Asia/Tokyo",
        "Pacific/Kiritimati",
        "Pacific/Pago_Pago",
        "Australia/Lord_Howe",
    ],
)
def test_cross_midnight_one_microsecond_either_side_of_local_midnight(tz: str) -> None:
    midnight = local(JUNE_15, time(0, 0), tz)
    just = timedelta(microseconds=1)
    assert daily_values([reading(midnight - just, tz=tz)])[0].day == date(2026, 6, 14)
    assert daily_values([reading(midnight, tz=tz)])[0].day == JUNE_15
    assert daily_values([reading(midnight + just, tz=tz)])[0].day == JUNE_15


def test_cross_midnight_the_evening_and_the_next_morning_are_different_days() -> None:
    values = daily_values(
        [
            reading(local(JUNE_15, time(23, 50)), "80.5"),
            reading(local(date(2026, 6, 16), time(0, 10)), "80.4"),
            reading(local(date(2026, 6, 16), time(6, 40)), "80.0"),
        ]
    )
    assert [(v.day, v.n, v.value, v.method) for v in values] == [
        (JUNE_15, 1, D("80.5"), DailyMethod.MEDIAN),
        (date(2026, 6, 16), 2, D("80.0"), DailyMethod.MORNING),
    ]


# --- travel across time zones -----------------------------------------------------------------


def test_travel_every_reading_uses_its_own_source_tz() -> None:
    instant = utc(2026, 6, 15, 2, 30)
    home = reading(instant, "80.0", tz="Europe/Stockholm")  # 04:30 on the 15th: morning
    new_york = reading(instant, "81.0", tz="America/New_York")  # 22:30 on the 14th
    tokyo = reading(instant, "82.0", tz="Asia/Tokyo")  # 11:30 on the 15th: not morning
    values = {v.day: v for v in daily_values([tokyo, new_york, home])}
    assert set(values) == {date(2026, 6, 14), JUNE_15}
    assert (values[date(2026, 6, 14)].value, values[date(2026, 6, 14)].method) == (
        D("81.0"),
        DailyMethod.MEDIAN,
    )
    assert (values[JUNE_15].value, values[JUNE_15].method, values[JUNE_15].n) == (
        D("80.0"),
        DailyMethod.MORNING,
        2,
    )


def test_travel_the_same_wall_clock_morning_is_morning_in_every_zone() -> None:
    for tz in ("Europe/Stockholm", "America/New_York", "Asia/Tokyo", "Pacific/Auckland"):
        value = one_day(reading(local(JUNE_15, time(7, 15), tz), "80.0", tz=tz))
        assert (value.day, value.method) == (JUNE_15, DailyMethod.MORNING), tz


def test_travel_the_first_morning_reading_is_the_earliest_instant() -> None:
    tokyo = reading(local(JUNE_15, time(7, 0), "Asia/Tokyo"), "82.0", tz="Asia/Tokyo")
    new_york = reading(
        local(JUNE_15, time(7, 0), "America/New_York"), "81.0", tz="America/New_York"
    )
    assert tokyo.measured_at < new_york.measured_at
    assert one_day(new_york, tokyo).value == D("82.0")


# --- exactness: no float rounding decides a day or a value ------------------------------------

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
ONE_US = timedelta(microseconds=1)
ZONES = (
    "Europe/Stockholm",
    "America/New_York",
    "Asia/Kolkata",
    "Pacific/Kiritimati",
    "Pacific/Pago_Pago",
    "Australia/Lord_Howe",
)


def exact_local_date(instant: datetime, tz: str) -> date:
    """The local date by integer microsecond arithmetic only (no float anywhere)."""
    offset = instant.astimezone(ZoneInfo(tz)).utcoffset()
    assert offset is not None
    local_us = (instant - EPOCH) // ONE_US + offset // ONE_US
    return date(1970, 1, 1) + timedelta(days=local_us // 86_400_000_000)


@pytest.mark.parametrize("tz", ZONES)
def test_day_assignment_matches_exact_integer_arithmetic_around_every_midnight(tz: str) -> None:
    checked = 0
    for offset in range(0, 366):
        day = date(2026, 1, 1) + timedelta(days=offset)
        midnight = local(day, time(0, 0), tz)
        for shift in (-3, -2, -1, 0, 1, 2, 3):
            instant = midnight + shift * ONE_US
            r = reading(instant, tz=tz)
            assert r.local_date == exact_local_date(instant, tz), (tz, instant)
            checked += 1
    assert checked == 366 * 7


@pytest.mark.parametrize("tz", ZONES)
def test_day_assignment_matches_exact_integer_arithmetic_on_random_instants(tz: str) -> None:
    rng = random.Random(20261031)  # noqa: S311 - seeded for reproducible tests, not for security
    first = int(utc(2026, 1, 1).timestamp()) * 1_000_000
    for _ in range(1500):
        instant = EPOCH + timedelta(microseconds=first + rng.randrange(366 * 86_400 * 1_000_000))
        assert daily_values([reading(instant, tz=tz)])[0].day == exact_local_date(instant, tz)


def test_values_are_exact_decimals_no_float_rounding() -> None:
    # In float 0.1 + 0.2 != 0.3; here nothing is rounded.
    value = one_day(
        reading(local(JUNE_15, time(12, 0)), "0.1"),
        reading(local(JUNE_15, time(13, 0)), "0.2"),
    )
    assert value.value == D("0.15")
    assert isinstance(value.value, Decimal)
    assert value.mad == D("0.05")
    assert value.value * 2 == D("0.3")
    many = [reading(local(JUNE_15, time(12, 0)) + i * ONE_US, "0.1") for i in range(10)]
    assert one_day(*many).value == D("0.1")
    assert sum((r.value for r in many), D(0)) == D(1)
