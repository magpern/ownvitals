"""B1, B3 and B6 (ADR 0010 §7; test specification "Rules"). Synthetic data only."""

from datetime import timedelta
from decimal import Decimal

import pytest
from rules_builders import DAY, alternating, dv, reading

from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules.body import Segment, b1, b3, b6, pair_indices, symmetry_index
from ownvitals.analysis.rules.core import (
    Band,
    Confidence,
    Finding,
    Insufficient,
    NoFinding,
    Provenance,
    Tier,
    finding_band,
)

D = DAY
D3 = D - timedelta(days=3)


def weights(start: str, end: str, *, middle: dict[int, str] | None = None) -> list:
    out = [dv(D3, start), dv(D, end)]
    out += [dv(D - timedelta(days=b), v) for b, v in (middle or {}).items()]
    return out


def ecw_history(centre: float = 15.0, spread: float = 0.5, *, last: float = 17.0) -> list:
    """90 days before d - 3 around ``centre``, plus ``last`` on d."""
    return [*alternating(D3 - timedelta(days=90), 90, centre, spread), dv(D, last)]


def b1_finding(**kw: object) -> Finding:
    result = b1(
        kw.get("w") or weights("80.0", "82.1"),  # type: ignore[arg-type]
        kw.get("e") or ecw_history(),  # type: ignore[arg-type]
        D,
        provenance=kw.get("p"),  # type: ignore[arg-type]
    )
    assert isinstance(result, Finding), result
    return result


def test_b1_fires_on_a_gain_over_two_kg_with_ecw_above_its_threshold() -> None:
    found = b1_finding()
    assert (found.rule_id, found.tier, found.subject) == ("B1", Tier.EXPERIMENTAL, "weight")
    assert (found.window_start, found.window_end, found.reference_day) == (D3, D, D3)
    assert found.evidence["gain_kg"] == "2.1"
    assert found.confidence is Confidence.HIGH  # 90 baseline days, no jump
    assert found.severity.value == "attention"


def test_b1_a_gain_of_exactly_two_kg_does_not_fire() -> None:
    assert isinstance(b1(weights("80.0", "82.0"), ecw_history(), D), NoFinding)
    assert isinstance(b1(weights("80.0", "82.1"), ecw_history(), D), Finding)


def test_b1_looks_back_only_three_days() -> None:
    far = [dv(D - timedelta(days=4), "80.0"), dv(D, "83.0")]
    assert isinstance(b1(far, ecw_history(), D), NoFinding)
    seven = [dv(D - timedelta(days=7), "70.0"), dv(D, "85.0")]  # the 7-day arm is not in v1
    assert isinstance(b1(seven, ecw_history(), D), NoFinding)


def test_b1_ecw_at_its_threshold_does_not_fire_and_just_above_does() -> None:
    band = finding_band("extracellular_water", ecw_history(), D3)
    assert isinstance(band, Band)
    at = Decimal(repr(band.high))
    assert isinstance(b1(weights("80.0", "82.1"), ecw_history(last=float(at)), D), NoFinding)
    above = ecw_history(last=float(at) + 0.01)
    assert isinstance(b1(weights("80.0", "82.1"), above, D), Finding)


def test_b1_ecw_without_a_baseline_is_insufficient_data_not_a_finding() -> None:
    sparse = [dv(D3 - timedelta(days=10 * i + 1), 15.0) for i in range(5)] + [dv(D, 30.0)]
    result = b1(weights("80.0", "82.1"), sparse, D)
    assert isinstance(result, Insufficient)
    assert "days_with_data" in result.shortfalls
    no_ecw_today = ecw_history()[:-1]
    assert isinstance(b1(weights("80.0", "82.1"), no_ecw_today, D), Insufficient)


def test_b1_the_largest_gain_picks_d0_and_a_tie_picks_the_earliest() -> None:
    d2 = D - timedelta(days=2)
    w = weights("80.0", "82.1", middle={2: "79.5", 1: "81.0"})
    found = b1_finding(w=w)
    assert found.reference_day == d2
    assert found.evidence["gain_kg"] == "2.6"
    tie = weights("80.0", "82.1", middle={2: "80.0"})
    assert b1_finding(w=tie).reference_day == D3


def test_b1_a_trigger_weight_with_jump_makes_it_low_but_an_ecw_flag_does_not() -> None:
    jumped = {"weight": {D: Provenance((7,), True)}}
    assert b1_finding(p=jumped).confidence is Confidence.LOW
    early = {"weight": {D3: Provenance((6,), True)}}
    assert b1_finding(p=early).confidence is Confidence.LOW
    water = {"extracellular_water": {D: Provenance((8,), True)}}
    found = b1_finding(p=water)
    assert found.confidence is Confidence.HIGH
    groups = {
        "weight": {D3: Provenance((6,)), D: Provenance((7,))},
        "extracellular_water": {D: Provenance((8,))},
    }
    assert b1_finding(p=groups).evidence["trigger_groups"] == [6, 7, 8]


def test_b1_needs_a_weight_on_the_evaluation_day() -> None:
    with pytest.raises(ValueError, match="no daily value"):
        b1([dv(D3, "80.0")], ecw_history(), D)


# --- B3 -----------------------------------------------------------------------------------------


def water(
    day: object, ecw: str, icw: str | None, *, grp: int, hour: int = 8
) -> tuple[Reading, Reading | None]:
    return (
        reading(day, ecw, grpid=grp, hour=hour),  # type: ignore[arg-type]
        None if icw is None else reading(day, icw, grpid=grp, hour=hour),  # type: ignore[arg-type]
    )


def split(*pairs: tuple[Reading, Reading | None]) -> tuple[list[Reading], list[Reading]]:
    return [e for e, _ in pairs], [i for _, i in pairs if i is not None]


def test_b3_fires_above_a_ratio_of_four_tenths_and_not_at_it() -> None:
    ecw, icw = split(water(D, "15.0", "22.0", grp=1))  # 15 / 37 = 0.4054
    found = b3(ecw, icw, D)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.confidence) == (
        "B3",
        Tier.EXPERIMENTAL,
        Confidence.HIGH,
    )
    assert found.evidence["ratio"] == pytest.approx(15 / 37)
    assert found.evidence["trigger_groups"] == [1]
    ecw, icw = split(water(D, "14.8", "22.2", grp=2))  # exactly 0.4
    assert isinstance(b3(ecw, icw, D), NoFinding)


def test_b3_a_group_without_icw_gives_no_ratio_and_a_user_without_icw_is_not_available() -> None:
    ecw, icw = split(
        water(D, "15.0", None, grp=1), water(D - timedelta(days=1), "15.0", "22.0", grp=2)
    )
    result = b3(ecw, icw, D)
    assert isinstance(result, Insufficient)
    assert "no measure group" in result.reason
    ecw_only, none = split(water(D, "15.0", None, grp=3))
    unavailable = b3(ecw_only, none, D)
    assert isinstance(unavailable, Insufficient)
    assert "not available" in unavailable.reason


def test_b3_never_pairs_ecw_and_icw_of_different_groups() -> None:
    ecw = [reading(D, "20.0", grpid=1)]
    icw = [reading(D, "22.0", grpid=2)]
    result = b3(ecw, icw, D)
    assert isinstance(result, Insufficient)
    assert "no measure group" in result.reason


def test_b3_the_earliest_morning_group_wins_otherwise_the_median_ratio() -> None:
    morning_low = water(D, "14.0", "23.0", grp=1, hour=8)  # 0.378
    evening_high = water(D, "17.0", "20.0", grp=2, hour=20)  # 0.459
    ecw, icw = split(morning_low, evening_high)
    assert isinstance(b3(ecw, icw, D), NoFinding)  # the morning group is the day's value
    ecw, icw = split(
        water(D, "17.0", "20.0", grp=2, hour=20),
        water(D, "17.0", "20.0", grp=3, hour=21),
        water(D, "14.0", "23.0", grp=4, hour=22),
    )
    median = b3(ecw, icw, D)
    assert isinstance(median, Finding)  # median of 0.459, 0.459, 0.378
    assert median.evidence["trigger_groups"] == [2, 3, 4]
    assert median.evidence["method"] == "median"


# --- B6 -----------------------------------------------------------------------------------------


def arms(days: list, left: str, right: str, first_grp: int = 100) -> list[Segment]:
    out: list[Segment] = []
    for i, day in enumerate(days):
        out.append(Segment(3, reading(day, left, grpid=first_grp + i)))
        out.append(Segment(2, reading(day, right, grpid=first_grp + i)))
    return out


def days_back(count: int, step: int = 1) -> list:
    return [D - timedelta(days=step * i) for i in range(count)]


def test_b6_arms_with_a_symmetry_index_over_ten_percent_fire() -> None:
    found = b6(arms(days_back(25), "3.00", "2.70"), D, pair="arms")  # SI 0.1053
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("B6", Tier.TREND, "arms")
    assert found.evidence["median_symmetry_index"] == pytest.approx(0.30 / 2.85)
    assert found.confidence is Confidence.HIGH  # 25 days with data, at least twice the minimum
    assert (found.window_start, found.window_end) == (D - timedelta(days=89), D)


def test_b6_confidence_is_high_from_twenty_days_with_data() -> None:
    twenty = b6(arms(days_back(20, 2), "3.00", "2.70"), D, pair="arms")  # span 38
    assert isinstance(twenty, Finding)
    assert twenty.confidence is Confidence.HIGH
    nineteen = b6(arms(days_back(19, 2), "3.00", "2.70"), D, pair="arms")
    assert isinstance(nineteen, Finding)
    assert nineteen.confidence is Confidence.MEDIUM


def test_b6_below_the_threshold_and_at_it_does_not_fire() -> None:
    assert isinstance(b6(arms(days_back(25), "3.00", "2.72"), D, pair="arms"), NoFinding)  # 0.0979
    assert isinstance(b6(arms(days_back(25), "21", "19"), D, pair="arms"), NoFinding)  # exactly 0.1


def test_b6_symmetry_index_is_exact_and_symmetric() -> None:
    assert symmetry_index(Decimal(21), Decimal(19)) == symmetry_index(Decimal(19), Decimal(21))
    assert symmetry_index(Decimal(0), Decimal(0)) is None


def test_b6_values_from_different_groups_are_never_paired() -> None:
    mixed = [Segment(3, reading(D, "3.00", grpid=1)), Segment(2, reading(D, "2.00", grpid=2))]
    assert pair_indices(mixed, "arms") == {}
    with pytest.raises(ValueError, match="no arms pair"):
        b6(mixed, D, pair="arms")


def test_b6_arms_and_legs_are_separate_subjects() -> None:
    legs = [
        *(Segment(10, reading(d, "9.0", grpid=500 + i)) for i, d in enumerate(days_back(25))),
        *(Segment(11, reading(d, "9.0", grpid=500 + i)) for i, d in enumerate(days_back(25))),
    ]
    segments = arms(days_back(25), "3.00", "2.70") + legs
    assert isinstance(b6(segments, D, pair="arms"), Finding)
    assert isinstance(b6(segments, D, pair="legs"), NoFinding)


def test_b6_the_median_of_the_window_ignores_one_outlier_group() -> None:
    base = arms(days_back(25), "3.00", "2.72")
    outlier = [
        Segment(3, reading(D - timedelta(days=1), "5.00", grpid=900)),
        Segment(2, reading(D - timedelta(days=1), "1.00", grpid=900)),
    ]
    assert isinstance(b6(base + outlier, D, pair="arms"), NoFinding)


def test_b6_keys_duplicates_on_grpid_and_position() -> None:
    segments = arms(days_back(25), "3.00", "2.70")
    twin = Segment(
        3, reading(D, "9.00", grpid=100)
    )  # same group and position, later in canonical order
    found = pair_indices([*segments, twin], "arms")
    assert found[100][1] == symmetry_index(Decimal("3.00"), Decimal("2.70"))  # the first one stays


def test_b6_sparse_data_is_insufficient() -> None:
    few = b6(arms(days_back(9), "3.00", "2.70"), D, pair="arms")
    assert isinstance(few, Insufficient)
    short_span = b6(
        arms([D - timedelta(days=2 * i) for i in range(10)], "3.00", "2.70"), D, pair="arms"
    )
    assert isinstance(short_span, Insufficient)  # 10 days spanning 18


def test_b6_groups_outside_the_window_do_not_count() -> None:
    old = arms([D - timedelta(days=89 + 5 + i) for i in range(25)], "3.00", "2.70", first_grp=700)
    recent = arms(days_back(25), "3.00", "2.72")
    assert isinstance(b6(old + recent, D, pair="arms"), NoFinding)
