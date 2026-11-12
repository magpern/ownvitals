import random
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from analysis_builders import reading, utc

from ownvitals.analysis import quality
from ownvitals.analysis.quality import (
    ENGINEERING,
    METRICS,
    Flag,
    Reading,
    UnknownMetricError,
    assess,
    from_epoch_seconds,
    real_value,
)

T0 = utc(2026, 6, 1, 6, 30)


def flags_of(result: quality.QualityResult, target: Reading) -> frozenset[Flag]:
    (found,) = (a for a in result.readings if a.reading is target)
    return found.flags


# --- attrib=1 -------------------------------------------------------------------------------


def test_attrib_one_is_kept_flagged_unconfirmed_and_not_used() -> None:
    own, other = reading(T0, "80.0"), reading(T0 + timedelta(hours=1), "55.0", attrib=1)
    result = assess("weight", [own, other])
    assert [a.reading for a in result.readings] == [own, other]  # nothing dropped
    assert flags_of(result, other) == {Flag.UNCONFIRMED}
    assert flags_of(result, own) == frozenset()
    assert result.usable == (own,)
    assert result.count(Flag.UNCONFIRMED) == 1


@pytest.mark.parametrize("attrib", [0, 2, 4, 5, 7, 8, 15])
def test_every_other_attrib_is_used(attrib: int) -> None:
    r = reading(T0, attrib=attrib)
    assert assess("weight", [r]).usable == (r,)


# --- plausibility ranges ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "plausible"),
    [
        ("9.99", False),
        ("10", True),
        ("10.01", True),
        ("299.99", True),
        ("300", True),
        ("300.01", False),
        ("0", False),
        ("-80", False),
    ],
)
def test_weight_range_is_inclusive_at_both_ends(value: str, plausible: bool) -> None:
    r = reading(T0, value)
    result = assess("weight", [r])
    assert (r in result.usable) is plausible
    assert (Flag.IMPLAUSIBLE in flags_of(result, r)) is (not plausible)
    assert [a.reading for a in result.readings] == [r]  # kept either way


def test_every_metric_range_cites_its_source_or_is_an_engineering_bound() -> None:
    for spec in METRICS.values():
        assert spec.bound == ENGINEERING or re.fullmatch(r"(findings|PLAN) §[\d.]+: .+", spec.bound)


def test_metric_table_is_consistent() -> None:
    assert all(key == spec.key for key, spec in METRICS.items())
    assert all(spec.low < spec.high for spec in METRICS.values())
    meastypes = [spec.meastype for spec in METRICS.values()]
    assert len(meastypes) == len(set(meastypes))
    assert all(spec.unit for spec in METRICS.values())


def test_a_metric_without_a_row_is_refused() -> None:
    with pytest.raises(UnknownMetricError):
        assess("not_a_metric", [reading(T0)])


@pytest.mark.parametrize("key", sorted(set(METRICS) - {"weight"}))
def test_only_weight_has_a_jump_rule(key: str) -> None:
    assert METRICS[key].jump is None


# --- jumps: max(2 kg, 3 %) within 24 h --------------------------------------------------------


def jumped(earlier: str, later: str, gap: timedelta = timedelta(hours=3)) -> bool:
    a, b = reading(T0, earlier), reading(T0 + gap, later)
    return Flag.JUMP in flags_of(assess("weight", [a, b]), b)


@pytest.mark.parametrize(
    ("earlier", "later", "expected"),
    [
        ("60.00", "62.00", False),  # 3 % is 1.80, so 2 kg applies: exactly at the limit
        ("60.00", "62.01", True),
        ("60.00", "57.99", True),
        ("60.00", "58.00", False),
        ("100.00", "103.00", False),  # 3 % is 3.00 and beats 2 kg: exactly at the limit
        ("100.00", "103.01", True),
        ("70.00", "72.10", False),  # 3 % is 2.10
        ("70.00", "72.11", True),
        ("66.66", "68.66", False),  # 3 % is 1.9998: the 2 kg floor applies
        ("66.66", "68.67", True),
    ],
)
def test_jump_threshold_is_max_of_two_kg_and_three_percent(
    earlier: str, later: str, expected: bool
) -> None:
    assert jumped(earlier, later) is expected


def test_the_three_percent_is_taken_of_the_earlier_reading() -> None:
    assert jumped("97.00", "100.00")  # limit 2.91, change 3.00
    assert not jumped("100.00", "97.00")  # limit 3.00, change 3.00


def test_jump_window_is_24_hours_inclusive() -> None:
    assert jumped("80.0", "86.0", gap=timedelta(hours=24))
    assert not jumped("80.0", "86.0", gap=timedelta(hours=24, seconds=1))
    assert jumped("80.0", "86.0", gap=timedelta(0))  # distinct readings at the same instant


def test_a_jump_is_advisory_the_reading_is_still_used() -> None:
    a, b = reading(T0, "80.0"), reading(T0 + timedelta(hours=2), "85.0")
    result = assess("weight", [a, b])
    assert flags_of(result, b) == {Flag.JUMP}
    assert flags_of(result, a) == frozenset()
    assert result.usable == (a, b)


def test_only_the_later_reading_of_a_pair_is_flagged_and_a_return_flags_again() -> None:
    a = reading(T0, "80.0")
    spike = reading(T0 + timedelta(hours=2), "86.0")
    back = reading(T0 + timedelta(hours=4), "80.1")
    result = assess("weight", [a, spike, back])
    assert flags_of(result, a) == frozenset()
    assert flags_of(result, spike) == {Flag.JUMP}
    assert flags_of(result, back) == {Flag.JUMP}  # 5.9 kg from the spike, 2 h earlier


def test_unconfirmed_and_implausible_readings_cause_no_jump_flags() -> None:
    mine = [reading(T0, "80.0"), reading(T0 + timedelta(hours=6), "80.4")]
    others = [
        reading(T0 + timedelta(hours=1), "55.0", attrib=1),  # a household member
        reading(T0 + timedelta(hours=2), "650.0"),  # broken scale
    ]
    result = assess("weight", [*mine, *others])
    assert result.count(Flag.JUMP) == 0
    assert result.usable == tuple(mine)


# --- duplicates -------------------------------------------------------------------------------


def test_only_an_identical_grpid_is_a_duplicate() -> None:
    first = reading(T0, "80.0", grpid=7)
    again = reading(T0, "80.0", grpid=7)  # re-delivered
    repeat = reading(T0, "80.0", grpid=8)  # same instant and value, but a distinct measurement
    clustered = reading(T0 + timedelta(minutes=2), "80.1", grpid=9)
    result = assess("weight", [again, repeat, clustered, first])
    assert result.count(Flag.DUPLICATE) == 1
    assert [a.reading.grpid for a in result.readings if a.usable] == [7, 8, 9]
    assert len(result.readings) == 4  # the duplicate is kept and flagged too


def test_of_two_readings_with_one_grpid_the_canonical_first_is_the_original() -> None:
    early = reading(T0, "80.0", grpid=5)
    late = reading(T0 + timedelta(minutes=1), "80.5", grpid=5)
    for order in ([early, late], [late, early]):
        result = assess("weight", order)
        assert result.usable == (early,)
        assert result.flagged(Flag.DUPLICATE) == (late,)


# --- determinism ------------------------------------------------------------------------------


def test_result_does_not_depend_on_input_order() -> None:
    rng = random.Random(7)  # noqa: S311 - seeded for reproducible tests, not for security
    items = [
        reading(T0 + timedelta(hours=h), v, attrib=a, grpid=g)
        for g, (h, v, a) in enumerate(
            [(0, "80.0", 0), (1, "55.0", 1), (2, "86.0", 0), (2, "80.2", 0), (30, "700", 0)]
        )
    ]
    expected = assess("weight", items)
    for _ in range(10):
        shuffled = items[:]
        rng.shuffle(shuffled)
        assert assess("weight", shuffled) == expected


def test_the_result_carries_the_algorithm_version() -> None:
    assert assess("weight", []).algo_version == quality.ALGO_VERSION
    assert re.fullmatch(r"quality/\d+", quality.ALGO_VERSION)


# --- units and exactness ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "unit", "expected"),
    [
        (79950, -3, "79.95"),
        (8, 1, "80"),
        (-5, -1, "-0.5"),
        (0, -6, "0"),
        (123456789012345678, -10, "12345678.9012345678"),
    ],
)
def test_real_value_applies_the_unit_exactly(raw: int, unit: int, expected: str) -> None:
    assert real_value(raw, unit) == Decimal(expected)


@pytest.mark.parametrize("bad", [79.95, True, "8", None])
def test_real_value_refuses_non_integers(bad: object) -> None:
    with pytest.raises(TypeError):
        real_value(bad, -2)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        real_value(1, bad)  # type: ignore[arg-type]


def test_float_epoch_seconds_can_cross_midnight_so_they_are_refused() -> None:
    midnight = datetime(2026, 6, 14, 22, 0, tzinfo=UTC)  # 00:00 on 15 June in Stockholm
    just_before = midnight.timestamp() - 1e-7  # 100 ns before midnight, finer than a float holds
    assert just_before == midnight.timestamp()  # the float cannot tell the two apart
    assert datetime.fromtimestamp(just_before, UTC) == midnight  # so it lands on the next day
    with pytest.raises(TypeError):
        from_epoch_seconds(just_before)  # type: ignore[arg-type]
    assert from_epoch_seconds(int(midnight.timestamp()) - 1) == midnight - timedelta(seconds=1)
    with pytest.raises(TypeError):
        from_epoch_seconds(True)


def test_readings_are_validated_at_the_boundary() -> None:
    ok = {"grpid": 1, "measured_at": T0, "source_tz": "UTC", "attrib": 0, "value": Decimal(80)}
    assert Reading(**ok).measured_at == T0  # type: ignore[arg-type]
    cases: list[tuple[dict[str, object], type[Exception]]] = [
        ({"measured_at": datetime(2026, 6, 1, 6, 30)}, ValueError),  # naive
        ({"source_tz": "Mars/Olympus"}, ValueError),
        ({"source_tz": "../etc/passwd"}, ValueError),
        ({"source_tz": ""}, ValueError),
        ({"value": 80.0}, TypeError),  # float
        ({"value": 80}, TypeError),  # int
        ({"value": Decimal("NaN")}, ValueError),
        ({"value": Decimal("Infinity")}, ValueError),
        ({"value": Decimal("1E+40")}, ValueError),
        ({"value": Decimal("1." + "0" * 40)}, ValueError),
        ({"grpid": True}, TypeError),
        ({"grpid": "7"}, TypeError),
        ({"attrib": 1.0}, TypeError),
    ]
    for override, error in cases:
        with pytest.raises(error):
            Reading(**{**ok, **override})  # type: ignore[arg-type]


def test_an_aware_instant_in_any_offset_is_normalised_to_utc() -> None:
    plus_two = datetime(2026, 6, 1, 8, 30, tzinfo=timezone(timedelta(hours=2)))
    r = Reading(1, plus_two, "UTC", 0, Decimal(80))
    assert r.measured_at.utcoffset() == timedelta(0)
    assert r.measured_at == plus_two
