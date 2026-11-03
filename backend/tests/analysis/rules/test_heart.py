"""C1 and C1x (ADR 0010 §3, §7; test specification "Rules"). Synthetic data only."""

from datetime import timedelta

import pytest
from rules_builders import DAY, alternating, constant, dv, run_of

from ownvitals.analysis.rules.core import (
    Confidence,
    Finding,
    Insufficient,
    NoFinding,
    Provenance,
    Tier,
)
from ownvitals.analysis.rules.heart import c1, c1x, run_ending_at

KEY = "heart_pulse"


def baseline(value: float = 61, *, end: int = 3, days: int = 90) -> list:
    """``days`` constant days ending ``end`` days before DAY."""
    return constant(DAY - timedelta(days=end + days - 1), days, value)


def with_run(values: list[float], *, base: list | None = None, gap_days: int = 0) -> list:
    """The baseline followed by ``values`` on the days ending at DAY."""
    first = DAY - timedelta(days=len(values) - 1)
    return [*(base if base is not None else baseline(end=len(values))), *run_of(first, values)]


def test_c1_three_days_of_71_72_71_over_a_baseline_of_61_fires_on_the_third() -> None:
    found = c1(with_run([71, 72, 71]), DAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("C1", Tier.TREND, KEY)
    assert (found.window_start, found.window_end) == (DAY - timedelta(days=2), DAY)
    assert found.reference_day == DAY - timedelta(days=2)
    assert found.confidence is Confidence.MEDIUM  # a run of 3 is under 6
    assert found.evidence["display_band"]["low"] == 59.0
    assert found.evidence["display_band"]["high"] == 63.0
    assert found.severity.value == "attention"


def test_c1_the_value_must_be_ten_or_more_above_the_median() -> None:
    assert isinstance(c1(with_run([71, 71, 71]), DAY), Finding)  # 61 + 10: inclusive
    assert isinstance(c1(with_run([71, 72, 70]), DAY), NoFinding)  # 70 = median + 9
    assert isinstance(c1(with_run([70, 70, 70]), DAY), NoFinding)


def test_c1_a_missing_middle_day_breaks_the_run() -> None:
    data = [*baseline(end=3), dv(DAY - timedelta(days=2), 71), dv(DAY, 72)]
    assert isinstance(c1(data, DAY), NoFinding)
    four = [
        *baseline(end=4),
        *run_of(DAY - timedelta(days=3), [72]),
        *run_of(DAY - timedelta(days=1), [72, 72]),
    ]
    assert isinstance(c1(four, DAY), NoFinding)  # 3 days after the gap would need 3 consecutive


def test_c1_a_noisy_baseline_needs_the_value_above_the_display_high() -> None:
    # 88 days alternating 53 / 69, then 61 twice: median 61, raw MAD 8
    noisy = [
        *alternating(DAY - timedelta(days=92), 88, 61, 8),
        dv(DAY - timedelta(days=4), 61),
        dv(DAY - timedelta(days=3), 61),
    ]
    high = 61 + 1.5 * 1.4826 * 8
    assert high > 71
    assert isinstance(c1([*noisy, *run_of(DAY - timedelta(days=2), [75, 75, 75])], DAY), NoFinding)
    fires = c1([*noisy, *run_of(DAY - timedelta(days=2), [80, 80, 80])], DAY)
    assert isinstance(fires, Finding)
    assert fires.evidence["display_band"]["high"] == pytest.approx(high, abs=0.01)


def test_c1_a_growing_run_keeps_the_band_of_its_first_day() -> None:
    first = c1(with_run([72, 72, 72]), DAY)
    assert isinstance(first, Finding)
    longer_data = [*baseline(end=6), *run_of(DAY - timedelta(days=5), [72] * 6)]
    longer = c1(longer_data, DAY)
    assert isinstance(longer, Finding)
    assert longer.reference_day == DAY - timedelta(days=5)
    assert longer.evidence["display_band"] == {
        **first.evidence["display_band"],
        "reference_day": (DAY - timedelta(days=5)).isoformat(),
    }
    assert longer.confidence is Confidence.HIGH  # 6 days
    assert longer.evidence["run_days"] == 6


def test_c1_runs_of_five_are_medium_and_six_high() -> None:
    five = c1([*baseline(end=5), *run_of(DAY - timedelta(days=4), [72] * 5)], DAY)
    assert isinstance(five, Finding)
    assert five.confidence is Confidence.MEDIUM


def test_c1_a_short_baseline_window_is_medium_even_for_a_long_run() -> None:
    thin = [dv(DAY - timedelta(days=9 + 3 * i), 61) for i in range(10)]
    found = c1([*thin, *run_of(DAY - timedelta(days=5), [72] * 6)], DAY)
    assert isinstance(found, Finding)
    assert found.confidence is Confidence.MEDIUM  # 10 days with data < 20


def test_c1_without_a_baseline_is_insufficient_data() -> None:
    sparse = [dv(DAY - timedelta(days=10 + 10 * i), 61) for i in range(5)]
    result = c1([*sparse, *run_of(DAY - timedelta(days=2), [72, 72, 72])], DAY)
    assert isinstance(result, Insufficient)
    assert "days_with_data" in result.shortfalls


def test_c1_carries_the_trigger_groups_of_the_run() -> None:
    p = {KEY: {DAY - timedelta(days=i): Provenance((10 + i,)) for i in range(3)}}
    found = c1(with_run([71, 72, 71]), DAY, provenance=p)
    assert isinstance(found, Finding)
    assert found.evidence["trigger_groups"] == [10, 11, 12]


def test_c1x_three_days_below_50_fire_as_an_experimental_finding() -> None:
    found = c1x(run_of(DAY - timedelta(days=2), [49, 48, 49]), DAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("C1x", Tier.EXPERIMENTAL, KEY)
    assert found.evidence["direction"] == "below"
    assert found.confidence is Confidence.MEDIUM
    assert (found.window_start, found.reference_day) == (DAY - timedelta(days=2),) * 2


def test_c1x_50_is_not_below_and_100_is_not_above() -> None:
    assert isinstance(c1x(run_of(DAY - timedelta(days=2), [50, 49, 49]), DAY), NoFinding)
    assert isinstance(c1x(run_of(DAY - timedelta(days=2), [49, 49, 50]), DAY), NoFinding)
    assert isinstance(c1x(run_of(DAY - timedelta(days=2), [101, 101, 101]), DAY), Finding)
    assert isinstance(c1x(run_of(DAY - timedelta(days=2), [101, 101, 100]), DAY), NoFinding)
    assert isinstance(c1x(run_of(DAY - timedelta(days=2), [100, 100, 100]), DAY), NoFinding)


def test_c1x_a_run_with_both_directions_is_no_run() -> None:
    assert isinstance(c1x(run_of(DAY - timedelta(days=2), [49, 101, 49]), DAY), NoFinding)
    assert isinstance(c1x(run_of(DAY - timedelta(days=2), [101, 49, 101]), DAY), NoFinding)


def test_c1x_a_gap_breaks_the_run_and_a_long_run_is_high() -> None:
    gapped = [dv(DAY - timedelta(days=3), 49), dv(DAY - timedelta(days=1), 49), dv(DAY, 49)]
    assert isinstance(c1x(gapped, DAY), NoFinding)
    long = c1x(run_of(DAY - timedelta(days=5), [105] * 6), DAY)
    assert isinstance(long, Finding)
    assert long.confidence is Confidence.HIGH
    assert long.evidence["direction"] == "above"


def test_c1_and_c1x_have_their_own_rule_ids() -> None:
    data = with_run([49, 49, 49])
    assert isinstance(c1(data, DAY), NoFinding)
    other = c1x(data, DAY)
    assert isinstance(other, Finding)
    assert other.rule_id == "C1x"


def test_run_ending_at_is_the_maximal_consecutive_run() -> None:
    data = [dv(DAY - timedelta(days=5), 1), *run_of(DAY - timedelta(days=3), [1, 1, 1, 1])]
    assert [v.day for v in run_ending_at(data, DAY)] == [
        DAY - timedelta(days=3 - i) for i in range(4)
    ]


def test_c1_at_the_baseline_edge_is_insufficient_below_it_and_decided_at_it() -> None:
    run = run_of(DAY - timedelta(days=2), [72, 72, 72])
    # 10 days with data spanning 27 days before the run: the BASELINE policy is met
    ok = [dv(DAY - timedelta(days=3 + 3 * i), 61) for i in range(10)]
    assert isinstance(c1([*ok, *run], DAY), Finding)
    calm = [dv(DAY - timedelta(days=3 + 3 * i), 61) for i in range(10)]
    assert isinstance(c1([*calm, *run_of(DAY - timedelta(days=2), [62, 62, 62])], DAY), NoFinding)
    # one day fewer, and the same run is insufficient data, not "no finding"
    short = ok[:-1]
    assert isinstance(c1([*short, *run], DAY), Insufficient)
    assert isinstance(
        c1([*short, *run_of(DAY - timedelta(days=2), [62, 62, 62])], DAY), Insufficient
    )
