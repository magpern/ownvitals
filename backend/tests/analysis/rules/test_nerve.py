"""N1 and N2 (ADR 0010 §3, §7; test specification "Rules", N1-N3). Synthetic data only."""

from datetime import timedelta

import pytest
from nerve_builders import GUIDED, daily_readings, nreading, weekly
from rules_builders import DAY

from ownvitals.analysis.daily import daily_values
from ownvitals.analysis.policy import InsufficientData, Shortfall, Sufficient
from ownvitals.analysis.rules.core import (
    Confidence,
    Finding,
    Insufficient,
    NoFinding,
    Provenance,
    Tier,
)
from ownvitals.analysis.rules.nerve import n1, n2_guided, n2_routine

KEY = "nerve_health_score"


def base(last=DAY - timedelta(days=7), n: int = 12, value: float = 70.0) -> list:
    """A weekly constant baseline: the finding threshold is 70 - 1.0 = 69.0 exactly (the metric's
    minimum half-width, MAD 0)."""
    return weekly(last, n, value)


def test_n1_three_measures_over_28_days_is_insufficient_data_and_four_run() -> None:
    three = [nreading(DAY - timedelta(days=d), 70) for d in (28, 14, 0)]
    four = [nreading(DAY - timedelta(days=d), 70) for d in (28, 21, 14, 0)]
    short = n1(KEY, daily_values(three), DAY)
    assert isinstance(short, InsufficientData)
    assert Shortfall.DAYS_WITH_DATA in short.shortfalls
    assert isinstance(n1(KEY, daily_values(four), DAY), Sufficient)


def test_n1_four_measures_within_27_days_are_insufficient_data() -> None:
    four = [nreading(DAY - timedelta(days=d), 70) for d in (27, 18, 9, 0)]
    result = n1(KEY, daily_values(four), DAY)
    assert isinstance(result, InsufficientData)
    assert result.shortfalls == (Shortfall.SPAN_DAYS,)


def test_n1_is_not_a_finding_and_refuses_a_metric_that_is_not_a_nerve_metric() -> None:
    assert not isinstance(n1(KEY, daily_values(base()), DAY), Finding)
    with pytest.raises(ValueError, match="not a nerve metric"):
        n1("weight", daily_values(base()), DAY)


def test_n1_gates_n2_so_that_three_measures_over_28_days_run_no_rule() -> None:
    three = [nreading(DAY - timedelta(days=d), 70) for d in (28, 14)]
    three.append(nreading(DAY, 10, attrib=GUIDED))
    result = n2_guided(three, DAY, metric_key=KEY)
    assert isinstance(result, Insufficient)
    assert "N1" in result.reason


def test_n2_guided_one_reading_below_the_threshold_fires_on_its_day() -> None:
    data = [*base(), nreading(DAY, 68.9, attrib=GUIDED, hour=9)]
    found = n2_guided(data, DAY, metric_key=KEY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("N2:guided", Tier.TREND, KEY)
    assert (found.window_start, found.window_end, found.reference_day) == (DAY, DAY, DAY)
    assert found.severity.value == "attention"
    assert found.evidence["threshold"] == 69.0
    assert len(found.evidence["trigger_groups"]) == 1
    assert found.confidence is Confidence.MEDIUM  # 12 baseline days are under twice the minimum


def test_n2_guided_fires_even_when_a_routine_reading_the_same_morning_is_the_daily_value() -> None:
    routine = nreading(DAY, 70, hour=7)  # the earliest morning reading is the daily value
    data = [*base(), routine, nreading(DAY, 60, attrib=GUIDED, hour=9)]
    assert next(v for v in daily_values(data) if v.day == DAY).value == 70
    assert isinstance(n2_guided(data, DAY, metric_key=KEY), Finding)


def test_n2_guided_at_the_threshold_does_not_fire() -> None:
    at = [*base(), nreading(DAY, 69.0, attrib=GUIDED)]
    assert isinstance(n2_guided(at, DAY, metric_key=KEY), NoFinding)
    above = [*base(), nreading(DAY, 80, attrib=GUIDED)]
    assert isinstance(n2_guided(above, DAY, metric_key=KEY), NoFinding)


def test_n2_guided_a_routine_reading_below_the_threshold_is_not_guided() -> None:
    data = [*base(), nreading(DAY, 60, attrib=0), nreading(DAY, 70, attrib=GUIDED, hour=9)]
    assert isinstance(n2_guided(data, DAY, metric_key=KEY), NoFinding)


def test_n2_guided_needs_a_guided_reading_on_the_day() -> None:
    with pytest.raises(ValueError, match="no guided"):
        n2_guided([*base(), nreading(DAY, 60)], DAY, metric_key=KEY)


def test_n2_guided_the_baseline_includes_guided_and_routine_readings_alike() -> None:
    guided_baseline = weekly(DAY - timedelta(days=7), 12, 70.0, attrib=GUIDED)
    data = [*guided_baseline, nreading(DAY, 68.9, attrib=GUIDED)]
    assert isinstance(n2_guided(data, DAY, metric_key=KEY), Finding)


def test_n2_guided_without_a_baseline_is_insufficient_data() -> None:
    data = [*weekly(DAY - timedelta(days=7), 5, 70.0), nreading(DAY, 60, attrib=GUIDED)]
    result = n2_guided(data, DAY, metric_key=KEY)
    assert isinstance(result, Insufficient)
    assert "baseline" in result.reason
    assert Shortfall.DAYS_WITH_DATA.value in result.shortfalls


def test_n2_routine_two_consecutive_measure_days_below_fire() -> None:
    data = [
        *base(DAY - timedelta(days=14)),
        nreading(DAY - timedelta(days=7), 68),
        nreading(DAY, 68),
    ]
    found = n2_routine(data, DAY, metric_key=KEY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.subject) == ("N2:routine", KEY)
    assert found.window_start == found.reference_day == DAY - timedelta(days=7)
    assert found.window_end == DAY
    assert found.evidence["run_measure_days"] == 2
    assert found.confidence is Confidence.MEDIUM


def test_n2_routine_one_day_below_does_not_fire() -> None:
    data = [
        *base(DAY - timedelta(days=14)),
        nreading(DAY - timedelta(days=7), 70),
        nreading(DAY, 68),
    ]
    assert isinstance(n2_routine(data, DAY, metric_key=KEY), NoFinding)


def test_n2_routine_a_10_day_gap_between_measures_still_counts_as_consecutive() -> None:
    last = DAY - timedelta(days=17)
    data = [*base(last), nreading(DAY - timedelta(days=10), 68), nreading(DAY, 68)]
    found = n2_routine(data, DAY, metric_key=KEY)
    assert isinstance(found, Finding)
    assert found.reference_day == DAY - timedelta(days=10)


def test_n2_routine_at_the_threshold_does_not_fire() -> None:
    data = [
        *base(DAY - timedelta(days=14)),
        nreading(DAY - timedelta(days=7), 69.0),
        nreading(DAY, 68),
    ]
    assert isinstance(n2_routine(data, DAY, metric_key=KEY), NoFinding)


def test_n2_routine_ignores_guided_readings_in_the_run() -> None:
    data = [
        *base(DAY - timedelta(days=14)),
        nreading(DAY - timedelta(days=7), 68, attrib=GUIDED),
        nreading(DAY, 68),
    ]
    assert isinstance(n2_routine(data, DAY, metric_key=KEY), NoFinding)  # 14 days ago was 70


def test_n2_routine_needs_a_routine_reading_on_the_day() -> None:
    data = [*base(), nreading(DAY, 60, attrib=GUIDED)]
    with pytest.raises(ValueError, match="no daily value"):
        n2_routine(data, DAY, metric_key=KEY)


def test_n2_routine_the_longest_run_keeps_its_first_day_and_band_and_is_high_from_four() -> None:
    data = [*daily_readings(DAY - timedelta(days=4), [70.0] * 85), *daily_readings(DAY, [68] * 4)]
    found = n2_routine(data, DAY, metric_key=KEY)
    assert isinstance(found, Finding)
    assert found.reference_day == DAY - timedelta(days=3)
    assert found.evidence["run_measure_days"] == 4
    assert found.confidence is Confidence.HIGH
    shorter = n2_routine(data[:-1], DAY - timedelta(days=1), metric_key=KEY)
    assert isinstance(shorter, Finding)
    assert shorter.confidence is Confidence.MEDIUM  # a run of 3 is under twice the minimum


def test_n2_routine_without_a_baseline_is_insufficient_data_and_carries_provenance() -> None:
    sparse = [*weekly(DAY - timedelta(days=14), 5, 70.0), nreading(DAY - timedelta(days=7), 68)]
    result = n2_routine([*sparse, nreading(DAY, 68)], DAY, metric_key=KEY)
    assert isinstance(result, Insufficient)
    data = [
        *base(DAY - timedelta(days=14)),
        nreading(DAY - timedelta(days=7), 68),
        nreading(DAY, 68),
    ]
    prov = {KEY: {DAY: Provenance(groups=(7,)), DAY - timedelta(days=7): Provenance(groups=(3,))}}
    found = n2_routine(data, DAY, metric_key=KEY, provenance=prov)
    assert isinstance(found, Finding)
    assert found.evidence["trigger_groups"] == [3, 7]


def test_n2_keys_are_never_mixed() -> None:
    other = [*base(), nreading(DAY, 60, attrib=GUIDED)]
    with pytest.raises(ValueError, match="not a nerve metric"):
        n2_guided(other, DAY, metric_key="heart_pulse")
    assert isinstance(n2_guided(other, DAY, metric_key="esc"), Finding)  # each key its own series
