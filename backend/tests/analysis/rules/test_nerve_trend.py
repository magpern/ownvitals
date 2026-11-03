"""N3 and N5 and the persistence of N3 (ADR 0010 §3, §4, §7; test specification "Rules" and
"Persistence of trend-test rules"). Synthetic data only: exact lines plus seeded noise."""

from datetime import timedelta

import pytest
from nerve_builders import foot_group, nreading, weekly_daily
from rules_builders import DAY, dv, fired, noise

from ownvitals.analysis.methods.trend import Direction
from ownvitals.analysis.policy import InsufficientData, Purpose, evaluate_metric
from ownvitals.analysis.rules.core import (
    Confidence,
    Finding,
    Insufficient,
    NoFinding,
    Provenance,
    Tier,
    trend_of,
)
from ownvitals.analysis.rules.episodes import Transition, TrendState, advance_persistent
from ownvitals.analysis.rules.nerve import n3, n5

KEY = "nerve_response_score"


def decline_180_only() -> list:
    """26 weekly points, a noisy straight decline (seed 2): significant on 180 days, not on 90."""
    e = noise(2, 26)
    return weekly_daily(DAY, [80 - 0.8 * i + 2.0 * e[i] for i in range(26)])


def decline_90_only() -> list:
    """A rise then a decline (26 weekly points): significant on the last 90 days only."""
    up = [60 + 20 * i / 12 for i in range(13)]
    return weekly_daily(DAY, up + [80 - 30 * i / 12 for i in range(1, 14)])


def test_n3_a_decline_significant_only_on_180_days_fires_with_window_180() -> None:
    data = decline_180_only()
    assert trend_of(data, DAY, 90, alpha=0.025).direction is Direction.NONE
    found = n3(data, DAY, metric_key=KEY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("N3", Tier.TREND, KEY)
    assert found.evidence["window_days"] == 180
    assert found.window_start == DAY - timedelta(days=179)
    assert found.window_end == found.reference_day == DAY
    assert found.severity.value == "attention"
    assert found.confidence is Confidence.HIGH  # 26 days with data, p far below alpha / 5


def test_n3_a_decline_significant_only_on_90_days_fires_with_window_90() -> None:
    data = decline_90_only()
    assert trend_of(data, DAY, 180, alpha=0.025).direction is Direction.NONE
    found = n3(data, DAY, metric_key=KEY)
    assert isinstance(found, Finding)
    assert found.evidence["window_days"] == 90
    assert found.window_start == DAY - timedelta(days=89)


def test_n3_when_both_windows_fire_the_180_days_are_the_window() -> None:
    e = noise(3, 26)
    data = weekly_daily(DAY, [100 - 2.0 * i + 1.0 * e[i] for i in range(26)])
    assert trend_of(data, DAY, 90, alpha=0.025).direction is Direction.DECREASING
    assert trend_of(data, DAY, 180, alpha=0.025).direction is Direction.DECREASING
    found = n3(data, DAY, metric_key=KEY)
    assert isinstance(found, Finding)
    assert found.evidence["window_days"] == 180


def test_n3_a_p_value_of_0_03_on_a_window_does_not_fire_at_alpha_0_025() -> None:
    e = noise(14, 13)
    data = weekly_daily(DAY, [70 - 0.5 * i + 2.0 * e[i] for i in range(13)])
    loose = trend_of(data, DAY, 90, alpha=0.05)
    assert loose.direction is Direction.DECREASING
    assert 0.025 < loose.p_value < 0.05
    assert isinstance(n3(data, DAY, metric_key=KEY), NoFinding)


def test_n3_a_rise_or_a_flat_series_does_not_fire() -> None:
    rise = weekly_daily(DAY, [60 + i for i in range(13)])
    flat = weekly_daily(DAY, [70.0] * 13)
    assert isinstance(n3(rise, DAY, metric_key=KEY), NoFinding)
    assert isinstance(n3(flat, DAY, metric_key=KEY), NoFinding)


def test_n3_four_measures_over_28_days_run_and_three_are_insufficient_data() -> None:
    four = [dv(DAY - timedelta(days=d), v) for d, v in ((28, 90), (19, 80), (10, 70), (0, 60))]
    assert isinstance(n3(four, DAY, metric_key=KEY), NoFinding)  # ran: n=4 cannot reach 0.025
    result = n3(four[1:], DAY, metric_key=KEY)
    assert isinstance(result, Insufficient)
    assert "N1" in result.reason
    assert result.shortfalls


def test_n3_a_window_below_n1_is_not_tested_while_the_other_is() -> None:
    e = noise(40, 16)  # seeded: the 180-day decline is significant at alpha 0.025
    old = [
        dv(DAY - timedelta(days=91 + 7 * i), round(60 + 2.5 * i + 2 * e[i], 2)) for i in range(13)
    ]
    recent = [
        dv(DAY - timedelta(days=d), round(v + 2 * e[13 + j], 2))
        for j, (d, v) in enumerate(((20, 55), (10, 52), (0, 50)))
    ]
    data = [*old, *recent]
    assert isinstance(
        evaluate_metric(KEY, Purpose.TREND, data, window_end=DAY, window_days=90), InsufficientData
    )
    found = n3(data, DAY, metric_key=KEY)
    assert isinstance(found, Finding)
    assert found.evidence["window_days"] == 180
    assert {"window_days": 90, "insufficient": True} in found.evidence["windows_tested"]


def test_n3_the_three_nerve_keys_are_never_mixed_and_others_are_refused() -> None:
    data = decline_90_only()
    assert n3(data, DAY, metric_key="esc").subject == "esc"
    with pytest.raises(ValueError, match="not a nerve metric"):
        n3(data, DAY, metric_key="weight")
    with pytest.raises(ValueError, match="no daily value"):
        n3(data, DAY + timedelta(days=1), metric_key=KEY)


def test_n3_carries_the_trigger_groups_of_the_day() -> None:
    found = n3(decline_90_only(), DAY, metric_key=KEY, provenance={KEY: {DAY: Provenance((9,))}})
    assert isinstance(found, Finding)
    assert found.evidence["trigger_groups"] == [9]


def test_n3_a_weekly_nerve_cadence_passes_the_persistence_test() -> None:
    """Persistence case 3: weekly evaluations on days 1, 8, 15, 22, 29, 36 open on the 6th."""
    state = TrendState()
    steps = []
    for day in (1, 8, 15, 22, 29, 36):
        when = DAY + timedelta(days=day)
        state, step = advance_persistent(
            state, fired(when, rule="N3", subject=KEY, window_days=90), today=when
        )
        steps.append(step.transition)
    assert steps[:-1] == [Transition.NONE] * 5  # 22 days but 4 evaluations; 29 days but 5
    assert steps[-1] is Transition.OPENED
    assert state.episode is not None
    assert state.episode.reference_day == DAY + timedelta(days=1)


def test_n3_an_insufficient_evaluation_breaks_the_run() -> None:
    state = TrendState()
    for day in (1, 8, 15):
        when = DAY + timedelta(days=day)
        state, _ = advance_persistent(state, fired(when, rule="N3", subject=KEY), today=when)
    gap = DAY + timedelta(days=22)
    state, step = advance_persistent(
        state, Insufficient("N3", KEY, gap, "nerve trend policy (N1)"), today=gap
    )
    assert state == TrendState()
    assert step.transition is Transition.NONE


# --- N5 ----------------------------------------------------------------------------------------


def pairs(left: float | str, right: float | str, *, weeks: int = 13, last=DAY) -> tuple[list, list]:
    groups = [foot_group(last - timedelta(days=7 * i), left, right) for i in range(weeks)]
    return [a for a, _ in groups], [b for _, b in groups]


def test_n5_pairs_with_si_0_25_in_every_group_fire() -> None:
    a, b = pairs(35, 45)  # |35 - 45| / 40 = 0.25
    found = n5(a, b, DAY, pair="158_159")
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("N5", Tier.TREND, "nerve_foot_158_159")
    assert found.evidence["median_symmetry_index"] == 0.25
    assert found.evidence["groups"] == 13
    assert found.window_start == DAY - timedelta(days=89)
    assert found.severity.value == "attention"
    assert len(found.evidence["trigger_groups"]) == 1


def test_n5_si_0_18_does_not_fire_and_0_20_exactly_does_not_either() -> None:
    a, b = pairs("43.6", "36.4")  # 7.2 / 40 = 0.18
    assert isinstance(n5(a, b, DAY, pair="158_159"), NoFinding)
    a, b = pairs(44, 36)  # 8 / 40 = 0.20: "more than"
    assert isinstance(n5(a, b, DAY, pair="158_159"), NoFinding)
    a, b = pairs("44.1", "35.9")  # 8.2 / 40 = 0.205
    assert isinstance(n5(a, b, DAY, pair="158_159"), Finding)


def test_n5_swapping_which_member_is_left_changes_nothing() -> None:
    a, b = pairs(35, 45)
    one, two = n5(a, b, DAY, pair="158_159"), n5(b, a, DAY, pair="158_159")
    assert isinstance(one, Finding)
    assert isinstance(two, Finding)
    assert one.evidence == two.evidence
    assert (one.confidence, one.window_start) == (two.confidence, two.window_start)


def test_n5_a_user_with_only_one_member_is_not_supported_never_a_finding() -> None:
    a, _ = pairs(35, 45)
    result = n5(a, [], DAY, pair="158_159")
    assert isinstance(result, Insufficient)
    assert result.reason.startswith("not supported")
    assert isinstance(n5([], a, DAY, pair="197_198"), Insufficient)


def test_n5_members_of_different_groups_are_never_paired() -> None:
    a = [nreading(DAY - timedelta(days=7 * i), 35, grpid=100 + i) for i in range(13)]
    b = [nreading(DAY - timedelta(days=7 * i), 45, grpid=200 + i) for i in range(13)]
    result = n5(a, b, DAY, pair="158_159")
    assert isinstance(result, Insufficient)
    assert result.reason.startswith("not supported")


def test_n5_the_days_with_a_pair_must_pass_n1() -> None:
    a, b = pairs(35, 45, weeks=3)
    result = n5(a, b, DAY, pair="158_159")
    assert isinstance(result, Insufficient)
    assert "N1" in result.reason
    assert result.shortfalls == ("days_with_data", "span_days")  # 3 groups over 14 days
    a, b = pairs(35, 45, weeks=4)  # four groups spanning 21 days: under 28
    assert n5(a, b, DAY, pair="158_159").shortfalls == ("span_days",)  # type: ignore[union-attr]
    a, b = pairs(35, 45, weeks=5)  # 28 days
    assert isinstance(n5(a, b, DAY, pair="158_159"), Finding)


def test_n5_the_two_pairs_are_separate_subjects_and_a_day_needs_a_pair() -> None:
    a, b = pairs(35, 45)
    c, d = pairs(45, 35)
    assert n5(a, b, DAY, pair="158_159").subject == "nerve_foot_158_159"  # type: ignore[union-attr]
    assert n5(c, d, DAY, pair="197_198").subject == "nerve_foot_197_198"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="no 158_159 pair"):
        n5(a, b, DAY + timedelta(days=1), pair="158_159")


def test_n5_a_duplicate_of_a_group_is_counted_once() -> None:
    a, b = pairs(35, 45)
    dup = nreading(DAY, 1, grpid=a[0].grpid, hour=9)  # a later duplicate of the same group
    found = n5([*a, dup], b, DAY, pair="158_159")
    assert isinstance(found, Finding)
    assert found.evidence["groups"] == 13
    assert found.confidence is Confidence.HIGH  # 13 days with data: over twice the minimum of 4
