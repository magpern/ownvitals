"""E1, E2 and E3 (ADR 0006 section 11; ADR 0010 section 7 "ECG"), and how the evaluation replays
them: documented codes only, a missing code never counted as negative, the backfill origin of an
E1 found in history. **Synthetic** recordings only: made-up signal ids, times and codes, no real
recording (AGENTS.md).
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from ownvitals.analysis.rules.core import (
    SPECS,
    Confidence,
    Finding,
    Insufficient,
    NoFinding,
    Severity,
    Tier,
)
from ownvitals.analysis.rules.ecg import (
    DETAIL_CODES,
    SUBJECT,
    Recording,
    decided_days,
    e1,
    e2,
    e3,
)
from ownvitals.derived.evaluate import Inputs, evaluate

SYNTHETIC = True
DAY = date(2026, 6, 30)
TODAY = date(2026, 7, 2)
_ids = iter(range(5000, 9000))


def rec(
    day: date = DAY,
    *,
    afib: int | None = None,
    details: int | None = None,
    hour: int = 8,
    groups: tuple[int, ...] = (),
) -> Recording:
    """A synthetic recording on ``day`` (UTC)."""
    return Recording(
        signalid=next(_ids),
        recorded_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
        day=day,
        afib=afib,
        afib_details=details,
        groups=groups,
    )


# --- the registry ------------------------------------------------------------------------------


def test_the_e_rules_have_the_adrs_tiers_and_severities() -> None:
    tiers = {r: SPECS[r].tier for r in ("E1", "E2", "E3", "E4", "E6")}
    assert tiers == {
        "E1": Tier.DEVICE,
        "E2": Tier.DEVICE,
        "E3": Tier.DEVICE,
        "E4": Tier.EXPERIMENTAL,
        "E6": Tier.TREND,
    }
    assert SPECS["E1"].severity is Severity.URGENT
    assert {SPECS[r].severity for r in ("E2", "E3", "E4", "E6")} == {Severity.ATTENTION}
    assert "E5" not in SPECS  # deferred (ADR 0006 section 12)
    assert not any(SPECS[r].persistent for r in tiers)  # they open on their first firing


# --- E1 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("afib", "details"), [(1, None), (None, 1), (None, 11), (None, 12), (0, 11), (1, 0)]
)
def test_e1_fires_on_afib_1_or_details_1_11_12(afib: int | None, details: int | None) -> None:
    found = e1([rec(afib=afib, details=details, groups=(7,))], DAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("E1", Tier.DEVICE, SUBJECT)
    assert (found.severity, found.confidence) == (Severity.URGENT, Confidence.HIGH)
    assert (found.window_start, found.window_end, found.reference_day) == (DAY, DAY, DAY)
    (held,) = found.evidence["recordings"]
    assert (held["afib"], held["afib_details"]) == (afib, details)  # both raw codes
    assert found.evidence["trigger_groups"] == [7]


@pytest.mark.parametrize(
    ("afib", "details"), [(0, None), (2, None), (None, 0), (None, 2), (None, 5), (0, 9), (2, 13)]
)
def test_e1_does_not_fire_on_other_documented_codes(afib: int | None, details: int | None) -> None:
    assert isinstance(e1([rec(afib=afib, details=details)], DAY), NoFinding)


@pytest.mark.parametrize(("afib", "details"), [(None, None), (3, None), (None, 3), (7, 99)])
def test_e1_a_missing_or_undocumented_code_is_never_negative(
    afib: int | None, details: int | None
) -> None:
    assert isinstance(e1([rec(afib=afib, details=details)], DAY), Insufficient)
    # A positive on the same day still fires; a documented negative does not settle it.
    assert isinstance(e1([rec(afib=afib, details=details), rec(afib=1)], DAY), Finding)
    assert isinstance(e1([rec(afib=afib, details=details), rec(afib=0)], DAY), Insufficient)


def test_e1_only_the_days_recordings_count_and_every_positive_is_a_trigger() -> None:
    data = [
        rec(DAY - timedelta(days=1), afib=1, groups=(1,)),
        rec(afib=0, groups=(2,)),
        rec(details=12, hour=9, groups=(3,)),
        rec(afib=1, hour=10, groups=(4, 5)),
    ]
    found = e1(data, DAY)
    assert isinstance(found, Finding)
    assert found.evidence["trigger_groups"] == [3, 4, 5]
    assert len(found.evidence["recordings"]) == 2


# --- E2 ----------------------------------------------------------------------------------------


def last(codes: list[int | None]) -> list[Recording]:
    """One recording a day ending on DAY, oldest first."""
    return [
        rec(DAY - timedelta(days=len(codes) - 1 - i), details=c, groups=(100 + i,))
        for i, c in enumerate(codes)
    ]


def test_e2_three_of_the_last_ten_inconclusive_or_noisy_fires() -> None:
    data = last([2, 0, 0, 5, 0, 0, 0, 2, 0, 0])
    found = e2(data, DAY)
    assert isinstance(found, Finding)
    assert (found.tier, found.severity) == (Tier.DEVICE, Severity.ATTENTION)
    assert found.evidence["of_last"] == 10
    assert found.evidence["trigger_groups"] == [100, 103, 107]
    assert (found.window_start, found.window_end) == (DAY - timedelta(days=9), DAY)


def test_e2_two_do_not_fire_and_only_the_last_ten_count() -> None:
    assert isinstance(e2(last([2, 0, 0, 5, 0, 0, 0, 0, 0, 0]), DAY), NoFinding)
    # The eleventh-latest is outside the window: two inside.
    assert isinstance(e2(last([2, 2, 0, 0, 5, 0, 0, 0, 0, 0, 0]), DAY), NoFinding)
    # Codes 1, 6, 7, 9 to 13, -1 are not inconclusive or noisy.
    assert isinstance(e2(last([1, 6, 7, 9, 10, 11, 12, 13, -1, 0]), DAY), NoFinding)


def test_e2_with_fewer_than_ten_the_same_count_over_those_that_exist() -> None:
    assert isinstance(e2(last([5, 2, 2]), DAY), Finding)
    assert isinstance(e2(last([5, 2]), DAY), NoFinding)


def test_e2_counts_up_to_the_evaluation_day_only() -> None:
    data = [*last([0, 0, 2, 5]), rec(DAY + timedelta(days=1), details=2)]
    assert isinstance(e2(data, DAY), NoFinding)
    assert isinstance(e2(data, DAY + timedelta(days=1)), Finding)


def test_e2_missing_codes_that_could_reach_three_leave_it_undecided() -> None:
    assert isinstance(e2(last([2, 5, None, 0]), DAY), Insufficient)  # the missing one may be 2
    assert isinstance(e2(last([2, None, 0, 0]), DAY), NoFinding)  # cannot reach 3
    assert isinstance(e2(last([2, 5, None, 2]), DAY), Finding)  # three documented ones


# --- E3 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("details", [6, 7])
def test_e3_fires_on_low_or_high_hr_reported(details: int) -> None:
    found = e3([rec(details=details, groups=(9,))], DAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.severity) == ("E3", Tier.DEVICE, Severity.ATTENTION)
    assert found.evidence["recordings"][0]["afib_details"] == details


def test_e3_other_codes_do_not_fire_and_a_missing_one_is_undecided() -> None:
    for details in DETAIL_CODES - {6, 7}:
        assert isinstance(e3([rec(details=details)], DAY), NoFinding), details
    assert isinstance(e3([rec(afib=0)], DAY), Insufficient)  # afib alone says nothing of HR
    assert isinstance(e3([rec(details=0), rec(details=None)], DAY), Insufficient)


# --- evaluation days and the replay --------------------------------------------------------------


def test_an_undecided_day_is_not_an_evaluation_day() -> None:
    d1, d2, d3 = DAY - timedelta(days=2), DAY - timedelta(days=1), DAY
    data = [rec(d1, afib=1), rec(d2), rec(d3, afib=0)]
    assert decided_days(e1, data) == (d1, d3)
    assert decided_days(e1, []) == ()


def inputs(recordings: list[Recording]) -> Inputs:
    return Inputs(today=TODAY, experimental=False, recordings=tuple(recordings))


def test_a_missing_code_between_two_positives_keeps_one_e1_episode() -> None:
    d = [DAY - timedelta(days=i) for i in (3, 2, 1)]
    data = [rec(d[0], afib=1), rec(d[1]), rec(d[2], details=11)]
    (episode,) = [e for e in evaluate(inputs(data)).episodes if e.rule_id == "E1"]
    assert (episode.first_day, episode.last_day, episode.status) == (d[0], d[2], "active")
    assert (episode.window_start, episode.window_end) == (d[0], d[2])


def test_a_negative_ends_e1_and_a_later_positive_opens_a_new_episode() -> None:
    d = [DAY - timedelta(days=i) for i in (3, 2, 1)]
    data = [rec(d[0], afib=1), rec(d[1], afib=0), rec(d[2], afib=1)]
    episodes = [e for e in evaluate(inputs(data)).episodes if e.rule_id == "E1"]
    assert [(e.reference_day, e.status) for e in episodes] == [(d[0], "ended"), (d[2], "active")]
    assert episodes[0].ended_day == d[1]


def test_an_e1_found_in_history_is_backfill_and_a_recent_one_live() -> None:
    old = DAY - timedelta(days=120)
    data = [rec(old, afib=1), rec(old + timedelta(days=1), afib=0), rec(TODAY, afib=1)]
    episodes = [e for e in evaluate(inputs(data)).episodes if e.rule_id == "E1"]
    assert [(e.reference_day, e.origin) for e in episodes] == [(old, "backfill"), (TODAY, "live")]


def test_the_device_rules_do_not_depend_on_the_switch() -> None:
    data = [rec(afib=1), *(rec(details=c, hour=9 + i) for i, c in enumerate((2, 5, 2, 7)))]
    off = evaluate(inputs(data))
    assert {e.rule_id for e in off.episodes} == {"E1", "E2", "E3"}
    assert {e.subject for e in off.episodes} == {SUBJECT}
    assert {e.metric_keys for e in off.episodes} == {()}


def test_no_recording_evaluates_no_device_rule() -> None:
    assert not {"E1", "E2", "E3"} & set(evaluate(inputs([])).evaluated)
