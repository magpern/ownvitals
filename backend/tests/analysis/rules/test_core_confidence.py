"""Confidence and severity (ADR 0010 §5 and §6; test specification "Confidence and severity").
Synthetic data only."""

from datetime import timedelta

import pytest
from rules_builders import DAY, constant, run_of

from ownvitals.analysis.methods.trend import Direction, TrendResult
from ownvitals.analysis.policy import Purpose, Sufficient, evaluate_metric
from ownvitals.analysis.rules.core import (
    SEVERITY,
    SPECS,
    Confidence,
    Severity,
    Tier,
    confidence_data,
    confidence_jump,
    confidence_run,
    confidence_trend,
    lowest,
    trend_of,
)

LOW, MEDIUM, HIGH = Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH


def sufficient(days: int, span: int) -> Sufficient:
    """A BASELINE verdict with ``days`` days with data (the first and last ``span`` apart)."""
    first = DAY - timedelta(days=89)
    data = constant(first, 1, 80)
    data += [d for d in constant(first + timedelta(days=span), 1, 80)]
    data += [d for d in run_of(first + timedelta(days=1), [80] * (days - 2))]
    verdict = evaluate_metric("weight", Purpose.BASELINE, data, window_end=DAY, window_days=90)
    assert isinstance(verdict, Sufficient), verdict
    return verdict


def test_d_exactly_twice_the_minimum_is_high_and_one_fewer_is_medium() -> None:
    assert confidence_data(sufficient(20, 40)) is HIGH
    assert confidence_data(sufficient(19, 40)) is MEDIUM
    assert confidence_data(sufficient(10, 40)) is MEDIUM


def test_d_needs_every_window_to_have_twice_the_minimum() -> None:
    assert confidence_data(sufficient(30, 60), sufficient(19, 40)) is MEDIUM
    assert confidence_data(sufficient(30, 60), sufficient(20, 40)) is HIGH


def trend_with(n: int, phi: float | None, p: float, alpha: float = 0.05) -> TrendResult:
    base = trend_of(run_of(DAY - timedelta(days=89), [float(i % 7) for i in range(60)]), DAY, 90)
    from dataclasses import replace

    inputs = replace(base.inputs, n=n)
    return replace(
        base, inputs=inputs, ar1_phi=phi, p_value=p, alpha=alpha, direction=Direction.NONE
    )


@pytest.mark.parametrize(
    ("n", "phi", "p", "expected"),
    [
        (19, 0.51, 0.001, LOW),
        (20, 0.51, 0.001, HIGH),  # n 20 is not low on T
        (19, 0.50, 0.001, HIGH),  # phi 0.50 is not above 0.5
        (19, None, 0.001, HIGH),
        (50, 0.9, 0.0099, HIGH),  # p just below alpha / 5 = 0.01
        (50, 0.9, 0.01, MEDIUM),  # p at alpha / 5
        (50, 0.9, 0.04, MEDIUM),
    ],
)
def test_t_trend_confidence(n: int, phi: float | None, p: float, expected: Confidence) -> None:
    assert confidence_trend(trend_with(n, phi, p)) is expected


@pytest.mark.parametrize(
    ("length", "minimum", "expected"),
    [(3, 3, MEDIUM), (5, 3, MEDIUM), (6, 3, HIGH), (2, 2, MEDIUM), (3, 2, MEDIUM), (4, 2, HIGH)],
)
def test_r_run_confidence(length: int, minimum: int, expected: Confidence) -> None:
    assert confidence_run(length, minimum) is expected


def test_j_a_jump_makes_it_low_whatever_the_rest() -> None:
    assert confidence_jump(True) is LOW
    assert confidence_jump(False) is HIGH
    assert lowest(HIGH, HIGH, confidence_jump(True)) is LOW


def test_the_lowest_criterion_wins() -> None:
    assert lowest(HIGH, MEDIUM, HIGH) is MEDIUM
    assert lowest(MEDIUM, LOW, HIGH) is LOW
    assert lowest(HIGH) is HIGH


def test_severity_is_urgent_only_for_e1_and_q1_reconnect_and_info_for_the_other_q1_parts() -> None:
    urgent = {rule for rule, s in SEVERITY.items() if s is Severity.URGENT}
    assert urgent == {"E1", "Q1:reconnect"}
    info = {rule for rule, s in SEVERITY.items() if s is Severity.INFO}
    assert info == {"Q1:no_weigh_in", "Q1:low_battery", "Q1:import_incomplete"}
    assert all(s is Severity.ATTENTION for rule, s in SEVERITY.items() if rule not in urgent | info)
    assert all(SEVERITY[rule] is spec.severity for rule, spec in SPECS.items())


def test_the_tiers_of_the_rules_follow_the_adr() -> None:
    experimental = {r for r, s in SPECS.items() if s.tier is Tier.EXPERIMENTAL}
    assert experimental == {"B1", "B3", "C1x"}
    persistent = {r for r, s in SPECS.items() if s.persistent}
    assert persistent == {"B2", "B4", "B5", "C2", "N3"}
    system = {r for r, s in SPECS.items() if s.tier is Tier.SYSTEM}
    assert system == {r for r in SPECS if r.startswith("Q1:")}
    assert all(s.tier is Tier.TREND for r, s in SPECS.items() if r not in experimental | system)
    assert all(spec.rule_id == rule and spec.version == 1 for rule, spec in SPECS.items())
