"""B2, B4, B5 and C2: the trend-test rules (ADR 0010 §3, §7; test specification "Rules").
Synthetic data only: straight lines plus seeded noise, or exact binary values."""

import inspect
from datetime import timedelta

import pytest
from rules_builders import DAY, dv, ending, noise, run_of

from ownvitals.analysis.methods.trend import Direction
from ownvitals.analysis.rules import heart
from ownvitals.analysis.rules.body import b2, b4, b5
from ownvitals.analysis.rules.core import (
    Confidence,
    Finding,
    Insufficient,
    NoFinding,
    Provenance,
    Tier,
)
from ownvitals.analysis.rules.heart import c2
from ownvitals.analysis.rules.trendrule import relative_change, trend_of


def ramp(change: float, days: int, level: float, sd: float, seed: int = 1) -> list:
    """A straight change of ``change`` (a fraction) over the window plus noise of sd ``sd``."""
    e = noise(seed, days)
    return ending(
        DAY, [round(level * (1 + change * i / (days - 1)) + sd * e[i], 3) for i in range(days)]
    )


def test_b2_fires_on_a_six_percent_decline_over_182_days() -> None:
    found = b2(ramp(-0.06, 182, 80.0, 0.2), DAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("B2", Tier.TREND, "weight")
    assert found.evidence["window_days"] == 182
    assert found.evidence["change_ratio"] <= -0.05
    assert found.window_start == DAY - timedelta(days=181)
    assert found.confidence is Confidence.HIGH


def test_b2_does_not_fire_on_a_four_percent_decline_nor_on_a_rise() -> None:
    four = ramp(-0.04, 182, 80.0, 0.2)
    assert isinstance(b2(four, DAY), NoFinding)
    result = trend_of(four, DAY, 182)  # significant, but under the effect threshold
    assert result.direction is Direction.DECREASING
    assert relative_change(result, 182) > -0.05
    assert isinstance(b2(ramp(0.06, 182, 80.0, 0.2), DAY), NoFinding)


def test_b2_a_decline_that_is_not_significant_does_not_fire() -> None:
    noisy = ramp(-0.06, 182, 80.0, 25.0, seed=4)
    assert trend_of(noisy, DAY, 182).direction is Direction.NONE
    assert isinstance(b2(noisy, DAY), NoFinding)


def test_b2_seven_days_with_data_is_insufficient_data() -> None:
    result = b2(ending(DAY, [80, 79, 78, 77, 76, 75, 74]), DAY)
    assert isinstance(result, Insufficient)
    assert set(result.shortfalls) >= {"days_with_data", "span_fraction"}


def test_b2_the_ratio_is_slope_times_182_over_the_fitted_level_exactly() -> None:
    five = ending(DAY, [100, 98, 99, 95, 96])
    result = trend_of(five, DAY, 5)
    # Theil-Sen over the 10 pairs: slope -1.0 / day wait for the hand computation below
    slopes = sorted(
        (b - a) / (j - i)
        for i, a in enumerate([100, 98, 99, 95, 96])
        for j, b in enumerate([100, 98, 99, 95, 96])
        if j > i
    )
    slope = (slopes[4] + slopes[5]) / 2
    intercept = sorted(y - slope * i for i, y in enumerate([100, 98, 99, 95, 96]))[2]
    assert result.slope == pytest.approx(slope)
    assert result.intercept == pytest.approx(intercept)
    assert relative_change(result, 182) == slope * 182 / intercept


def test_b2_a_trigger_weight_with_jump_is_low_whatever_the_rest() -> None:
    data = ramp(-0.06, 182, 80.0, 0.2)
    found = b2(data, DAY, provenance={"weight": {DAY: Provenance((5,), True)}})
    assert isinstance(found, Finding)
    assert found.confidence is Confidence.LOW
    assert found.evidence["trigger_groups"] == [5]


def exact_decline(days: int, intercept: float, slope: float) -> list:
    """Values exact in binary, so the Theil-Sen ratio is exact: -0.5 * 90 / 2250 = -0.02."""
    return ending(DAY, [intercept + slope * i for i in range(days)])


def test_b4_fires_on_a_two_and_a_half_percent_decline() -> None:
    found = b4(ramp(-0.025, 90, 30.0, 0.05, seed=2), DAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.subject, found.confidence) == (
        "B4",
        "muscle_mass",
        Confidence.HIGH,
    )
    assert isinstance(b4(ramp(-0.015, 90, 30.0, 0.05, seed=2), DAY), NoFinding)


def test_b4_the_boundary_of_minus_two_percent_exactly_fires() -> None:
    line = exact_decline(90, 2250.0, -0.5)
    assert relative_change(trend_of(line, DAY, 90), 90) == -0.02
    assert isinstance(b4(line, DAY), Finding)
    just_under = exact_decline(90, 2250.0, -0.4)  # -0.016
    assert isinstance(b4(just_under, DAY), NoFinding)
    over = exact_decline(90, 2250.0, -0.6)  # -0.024
    assert isinstance(b4(over, DAY), Finding)


def step_up(seed: int = 3) -> list:
    e = noise(seed, 90)
    return ending(DAY, [round(10 + (1 if i >= 45 else 0) + 0.1 * e[i], 3) for i in range(90)])


def test_b5_a_step_of_one_index_point_at_mid_window_fires_and_a_flat_series_does_not() -> None:
    found = b5(step_up(), DAY, metric_key="visceral_fat")
    assert isinstance(found, Finding)
    assert (found.rule_id, found.subject) == ("B5", "visceral_fat")
    assert isinstance(b5(ending(DAY, [10] * 90), DAY, metric_key="visceral_fat"), NoFinding)


def test_b5_visceral_fat_and_metabolic_age_are_separate_subjects() -> None:
    age = b5(step_up(), DAY, metric_key="metabolic_age")
    assert isinstance(age, Finding)
    assert age.subject == "metabolic_age"
    with pytest.raises(ValueError, match="visceral_fat or metabolic_age"):
        b5(step_up(), DAY, metric_key="weight")  # type: ignore[arg-type]


def test_c2_a_constant_vascular_age_is_not_rising_and_one_rising_a_year_a_month_is() -> None:
    assert isinstance(c2(ending(DAY, [50] * 90), DAY), NoFinding)
    rising = c2(ending(DAY, [round(50 + i / 30.4, 4) for i in range(90)]), DAY)
    assert isinstance(rising, Finding)
    assert (rising.rule_id, rising.subject) == ("C2", "vascular_age")


def test_c2_takes_no_birth_year() -> None:
    assert not any("birth" in name for name in inspect.signature(heart.c2).parameters)


def test_c2_shifting_every_value_by_a_constant_changes_nothing() -> None:
    base = [round(50 + i / 30.4, 4) for i in range(90)]
    a = c2(ending(DAY, base), DAY)
    b = c2(ending(DAY, [v + 17 for v in base]), DAY)
    assert isinstance(a, Finding)
    assert isinstance(b, Finding)
    assert a.evidence["trend"]["direction"] == b.evidence["trend"]["direction"]


def test_a_rule_is_evaluated_only_on_a_day_with_a_value() -> None:
    data = [dv(DAY - timedelta(days=i), 10) for i in range(1, 91)]
    with pytest.raises(ValueError, match="no daily value"):
        b5(data, DAY, metric_key="visceral_fat")
    assert run_of(DAY, [1])
