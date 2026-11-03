"""The overlay's counts (ADR 0006 section 11; the test specification's "The overlay, the read
function and the notification hold"): several trigger recordings, only the trigger counts, and
the trigger is the value's provenance. Synthetic only.

The trigger is ``evidence["trigger_groups"]`` as the rules write it (``analysis.rules.core``):
one group for a ``morning`` daily value, every group the median used otherwise. Baseline groups
are in the pool of live groups and recordings but never in the trigger, so ``resolve_links``
never links them.
"""

import pytest
from test_overlay import FAIL, NO_RESULT, PASS, group, linked, recording

from ownvitals.ecg.overlay import ecg_gate_overlay, resolve_links

SYNTHETIC = True


def _overlay(rule: str, triggers: list[int], strips: dict[int, dict[str, object] | None]):
    """``strips``: grpid -> the estimate of the one recording at that group's minute."""
    groups = [group(g, minutes=g) for g in strips]
    recordings = [recording(est, minutes=g) for g, est in strips.items()]
    return ecg_gate_overlay(rule, resolve_links(triggers, groups, recordings))


# --- E6 with several trigger recordings --------------------------------------------------------


@pytest.mark.parametrize(
    ("estimates", "hidden"),
    [
        ([FAIL, PASS], True),  # 1 of 2 failed: half
        ([FAIL, FAIL, PASS], True),  # 2 of 3
        ([FAIL, PASS, PASS], False),  # 1 of 3
        ([PASS, PASS], False),
    ],
)
def test_e6_hides_when_at_least_half_of_the_counted_failed(
    estimates: list[dict[str, object]], hidden: bool
) -> None:
    result = ecg_gate_overlay("E6", linked(*(recording(e) for e in estimates)))
    assert result.hidden is hidden
    assert result.counted == len(estimates)


def test_e6_with_no_gate_result_is_not_evaluated() -> None:
    result = ecg_gate_overlay(
        "E6", linked(recording(None), recording(NO_RESULT["signal_missing"]), None)
    )
    assert (result.hidden, result.outcome, result.counted) == (False, "not_evaluated", 0)


def test_an_unlinked_group_and_a_recording_without_a_result_stay_out_of_the_count() -> None:
    unlinked = ecg_gate_overlay("E6", linked(recording(FAIL), recording(PASS), None))
    no_result = ecg_gate_overlay(
        "E6", linked(recording(FAIL), recording(PASS), recording(NO_RESULT["no_estimated"]))
    )
    for result in (unlinked, no_result):
        assert (result.hidden, result.counted, result.failed) == (True, 2, 1)


def test_a_failure_never_counts_as_a_pass() -> None:
    # 1 failure with 1 pass is half: hidden. Were the failure a pass it would be shown.
    assert ecg_gate_overlay("E6", linked(recording(FAIL), recording(PASS))).hidden
    assert ecg_gate_overlay("E4", linked(recording(FAIL))).outcome == "failed"


# --- only the trigger counts, in both directions -----------------------------------------------


def test_a_failing_trigger_with_a_passing_baseline_hides_e6_and_e4() -> None:
    strips: dict[int, dict[str, object] | None] = {1: FAIL}
    strips |= {g: PASS for g in range(2, 12)}  # ten baseline strips, all passed
    assert _overlay("E6", [1], strips).hidden
    assert _overlay("E4", [1], strips).hidden


def test_a_passing_trigger_with_a_failing_baseline_shows_e6() -> None:
    strips: dict[int, dict[str, object] | None] = {1: PASS}
    strips |= {g: (FAIL if g <= 9 else PASS) for g in range(2, 12)}  # 8 of 10 failed
    result = _overlay("E6", [1], strips)
    assert (result.hidden, result.counted) == (False, 1)


@pytest.mark.parametrize("baseline", [PASS, FAIL])
def test_a_trigger_without_a_result_is_not_evaluated_whatever_the_baseline(
    baseline: dict[str, object],
) -> None:
    strips: dict[int, dict[str, object] | None] = {1: None}
    strips |= {g: baseline for g in range(2, 12)}
    assert _overlay("E6", [1], strips).outcome == "not_evaluated"


def test_a_trigger_group_also_in_a_later_baseline_counts_as_a_trigger() -> None:
    # A multi-day window: day 1's trigger (group 1) is also in day 2's baseline.
    strips: dict[int, dict[str, object] | None] = {1: FAIL, 2: PASS, 3: PASS}
    assert _overlay("E6", [1, 2], strips).hidden  # 1 of 2 failed


# --- the trigger is the value's provenance -----------------------------------------------------


def test_a_morning_value_counts_only_its_morning_strip() -> None:
    # Group 1 is the morning reading; 2 and 3 are retakes the same day.
    failing_morning: dict[int, dict[str, object] | None] = {1: FAIL, 2: PASS, 3: PASS}
    assert _overlay("E6", [1], failing_morning).hidden
    passing_morning: dict[int, dict[str, object] | None] = {1: PASS, 2: FAIL}
    assert not _overlay("E6", [1], passing_morning).hidden


def test_a_median_value_counts_every_strip_it_used() -> None:
    two_failed: dict[int, dict[str, object] | None] = {1: FAIL, 2: FAIL, 3: PASS}
    assert _overlay("E6", [1, 2, 3], two_failed).hidden
    one_failed: dict[int, dict[str, object] | None] = {1: FAIL, 2: PASS, 3: PASS}
    assert not _overlay("E6", [1, 2, 3], one_failed).hidden


def test_the_counted_set_is_exactly_the_provenance() -> None:
    day: dict[int, dict[str, object] | None] = {1: PASS, 2: FAIL, 3: FAIL}
    result = _overlay("E6", [1], day)
    assert (result.hidden, result.counted, result.failed) == (False, 1, 0)
