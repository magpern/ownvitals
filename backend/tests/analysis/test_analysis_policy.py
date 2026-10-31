import re
from datetime import date, timedelta
from fractions import Fraction

import pytest
from analysis_builders import daily_on, spread_days

from ownvitals.analysis import policy
from ownvitals.analysis.policy import (
    BASELINE_DEFAULT,
    DEFAULT_POLICY,
    NERVE_TREND,
    POLICIES,
    TREND_DEFAULT,
    InsufficientData,
    Purpose,
    Requirement,
    Shortfall,
    Sufficiency,
    Sufficient,
    evaluate,
    evaluate_metric,
    policy_for,
)
from ownvitals.analysis.quality import METRICS

END = date(2026, 6, 30)


def run(
    requirement: Requirement, window_days: int, span: int, days: int, *, end: date = END
) -> Sufficiency:
    """``days`` days with data spread over ``span`` calendar days at the start of the window."""
    first = end - timedelta(days=window_days - 1)
    return evaluate(
        daily_on(*spread_days(first, span, days)),
        requirement=requirement,
        window_end=end,
        window_days=window_days,
    )


def short(result: Sufficiency) -> tuple[Shortfall, ...]:
    assert isinstance(result, InsufficientData)
    return result.shortfalls


def ok(result: Sufficiency) -> bool:
    assert isinstance(result, Sufficient | InsufficientData)
    return isinstance(result, Sufficient)


# --- the defaults are the ones in PLAN §7.1 step 4 --------------------------------------------


def test_defaults_are_the_specified_ones() -> None:
    assert Requirement(8, min_span_days=None, min_span_fraction=Fraction(3, 5)) == TREND_DEFAULT
    assert Requirement(10, min_span_days=21, min_span_fraction=None) == BASELINE_DEFAULT
    assert DEFAULT_POLICY.trend is TREND_DEFAULT
    assert DEFAULT_POLICY.baseline is BASELINE_DEFAULT
    assert Requirement(4, min_span_days=28) == NERVE_TREND  # N1: 4 measures over 4 weeks


# --- trend: at least 8 days with data spanning at least 60 % of the window --------------------


@pytest.mark.parametrize(("days", "sufficient"), [(7, False), (8, True), (9, True)])
def test_trend_days_with_data_boundary_8(days: int, sufficient: bool) -> None:
    result = run(TREND_DEFAULT, window_days=30, span=18, days=days)  # span is at its minimum
    assert ok(result) is sufficient
    if not sufficient:
        assert short(result) == (Shortfall.DAYS_WITH_DATA,)


@pytest.mark.parametrize(("span", "sufficient"), [(17, False), (18, True), (19, True)])
def test_trend_span_boundary_60_percent_of_a_30_day_window(span: int, sufficient: bool) -> None:
    result = run(TREND_DEFAULT, window_days=30, span=span, days=10)
    assert ok(result) is sufficient
    if not sufficient:
        assert short(result) == (Shortfall.SPAN_FRACTION,)


@pytest.mark.parametrize(("span", "sufficient"), [(8, False), (9, True)])
def test_trend_span_boundary_when_60_percent_is_not_a_whole_day(
    span: int, sufficient: bool
) -> None:
    # 60 % of 14 days is 8.4: 8 days (57 %) fall short, 9 days (64 %) pass.
    assert ok(run(TREND_DEFAULT, window_days=14, span=span, days=8)) is sufficient


@pytest.mark.parametrize(("span", "sufficient"), [(5, False), (6, True), (7, True)])
def test_span_fraction_is_inclusive_exactly_at_60_percent(span: int, sufficient: bool) -> None:
    requirement = Requirement(2, min_span_fraction=Fraction(3, 5))
    assert ok(run(requirement, window_days=10, span=span, days=2)) is sufficient


def test_trend_with_both_shortfalls_reports_both() -> None:
    result = run(TREND_DEFAULT, window_days=30, span=5, days=3)
    assert short(result) == (Shortfall.DAYS_WITH_DATA, Shortfall.SPAN_FRACTION)


# --- baseline: at least 10 points over at least 21 days ---------------------------------------


@pytest.mark.parametrize(("days", "sufficient"), [(9, False), (10, True), (11, True)])
def test_baseline_points_boundary_10(days: int, sufficient: bool) -> None:
    result = run(BASELINE_DEFAULT, window_days=90, span=21, days=days)
    assert ok(result) is sufficient
    if not sufficient:
        assert short(result) == (Shortfall.DAYS_WITH_DATA,)


@pytest.mark.parametrize(("span", "sufficient"), [(20, False), (21, True), (22, True)])
def test_baseline_span_boundary_21_days(span: int, sufficient: bool) -> None:
    result = run(BASELINE_DEFAULT, window_days=90, span=span, days=10)
    assert ok(result) is sufficient
    if not sufficient:
        assert short(result) == (Shortfall.SPAN_DAYS,)


def test_the_span_counts_both_end_days() -> None:
    first = END - timedelta(days=89)
    twenty_one_days = [first, first + timedelta(days=20)]  # 1 June to 21 June, 21 calendar days
    twenty_days = [first, first + timedelta(days=19)]
    extra = [first + timedelta(days=d) for d in range(1, 9)]  # 8 more days, all in between
    sufficient = evaluate(
        daily_on(*twenty_one_days, *extra),
        requirement=BASELINE_DEFAULT,
        window_end=END,
        window_days=90,
    )
    insufficient = evaluate(
        daily_on(*twenty_days, *extra),
        requirement=BASELINE_DEFAULT,
        window_end=END,
        window_days=90,
    )
    assert isinstance(sufficient, Sufficient)
    assert isinstance(insufficient, InsufficientData)


# --- nerve metrics: N1 asks for 4 measures over 4 weeks ---------------------------------------


@pytest.mark.parametrize(("days", "sufficient"), [(3, False), (4, True), (5, True)])
def test_nerve_trend_measures_boundary_4(days: int, sufficient: bool) -> None:
    result = run(NERVE_TREND, window_days=90, span=28, days=days)
    assert ok(result) is sufficient
    if not sufficient:
        assert short(result) == (Shortfall.DAYS_WITH_DATA,)


@pytest.mark.parametrize(("span", "sufficient"), [(27, False), (28, True), (29, True)])
def test_nerve_trend_span_boundary_4_weeks(span: int, sufficient: bool) -> None:
    result = run(NERVE_TREND, window_days=90, span=span, days=4)
    assert ok(result) is sufficient
    if not sufficient:
        assert short(result) == (Shortfall.SPAN_DAYS,)


@pytest.mark.parametrize("key", ["nerve_health_score", "nerve_response_score", "esc"])
def test_nerve_metrics_use_n1_for_trends_and_the_default_for_baselines(key: str) -> None:
    assert policy_for(key).trend == NERVE_TREND
    assert policy_for(key).baseline == BASELINE_DEFAULT
    first = END - timedelta(days=89)
    four_over_four_weeks = daily_on(*spread_days(first, 28, 4))
    trend = evaluate_metric(
        key, Purpose.TREND, four_over_four_weeks, window_end=END, window_days=90
    )
    baseline = evaluate_metric(
        key, Purpose.BASELINE, four_over_four_weeks, window_end=END, window_days=90
    )
    assert isinstance(trend, Sufficient)
    assert isinstance(baseline, InsufficientData)


# --- declarative table ------------------------------------------------------------------------


def test_metrics_without_an_override_use_the_defaults() -> None:
    assert policy_for("weight") is DEFAULT_POLICY
    assert policy_for("a_metric_nobody_declared") is DEFAULT_POLICY


def test_every_override_belongs_to_a_known_metric() -> None:
    assert set(POLICIES) <= set(METRICS)


def test_the_policy_table_cannot_be_changed_at_runtime() -> None:
    with pytest.raises(TypeError):
        POLICIES["weight"] = DEFAULT_POLICY  # type: ignore[index]


def test_evaluate_metric_picks_the_requirement_of_the_purpose() -> None:
    first = END - timedelta(days=29)
    days = daily_on(*spread_days(first, 18, 8))
    trend = evaluate_metric("weight", Purpose.TREND, days, window_end=END, window_days=30)
    below = evaluate_metric("weight", Purpose.BASELINE, days, window_end=END, window_days=30)
    assert isinstance(trend, Sufficient)
    assert isinstance(below, InsufficientData)
    assert below.requirement == BASELINE_DEFAULT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_days_with_data": 0},
        {"min_days_with_data": 1, "min_span_days": 0},
        {"min_days_with_data": 1, "min_span_fraction": Fraction(0)},
        {"min_days_with_data": 1, "min_span_fraction": Fraction(6, 5)},
    ],
)
def test_a_nonsensical_requirement_is_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="must be"):
        Requirement(**kwargs)  # type: ignore[arg-type]


# --- the typed result -------------------------------------------------------------------------


def test_insufficient_data_carries_n_and_coverage_and_never_a_value() -> None:
    result = run(TREND_DEFAULT, window_days=30, span=10, days=5)
    assert isinstance(result, InsufficientData)
    assert result.n == 5
    assert result.coverage == Fraction(5, 30)
    assert result.data_quality.span_days == 10
    assert result.requirement == TREND_DEFAULT
    assert not hasattr(result, "value")
    assert not hasattr(result, "finding")
    assert result.algo_version == policy.ALGO_VERSION


def test_no_data_at_all_is_insufficient_with_every_shortfall() -> None:
    result = evaluate([], requirement=BASELINE_DEFAULT, window_end=END, window_days=90)
    assert isinstance(result, InsufficientData)
    assert (result.n, result.coverage) == (0, 0)
    assert result.shortfalls == (Shortfall.DAYS_WITH_DATA, Shortfall.SPAN_DAYS)


def test_sufficient_data_carries_the_data_quality() -> None:
    result = run(TREND_DEFAULT, window_days=30, span=20, days=12)
    assert isinstance(result, Sufficient)
    assert (result.data_quality.n_days, result.data_quality.span_days) == (12, 20)


def test_only_days_inside_the_window_count() -> None:
    first = END - timedelta(days=29)
    inside = spread_days(first, 18, 7)  # one day short of 8
    outside = [first - timedelta(days=d) for d in range(1, 6)] + [END + timedelta(days=1)]
    result = evaluate(
        daily_on(*inside, *outside), requirement=TREND_DEFAULT, window_end=END, window_days=30
    )
    assert short(result) == (Shortfall.DAYS_WITH_DATA,)
    assert isinstance(result, InsufficientData)
    assert result.n == 7


def test_the_policy_is_deterministic() -> None:
    assert run(TREND_DEFAULT, 30, 18, 8) == run(TREND_DEFAULT, 30, 18, 8)
    assert re.fullmatch(r"policy/\d+", policy.ALGO_VERSION)
