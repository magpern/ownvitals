"""Episodes, origin and the persistence of trend-test rules (ADR 0010 §4; test specification
"Episodes and origin" and "Persistence of trend-test rules"). The trend test is replaced by a
scripted condition. Synthetic data only."""

from collections.abc import Sequence
from datetime import date, timedelta

import pytest
from rules_builders import fired

from ownvitals.analysis.rules.core import (
    Finding,
    Insufficient,
    NoFinding,
    RuleResult,
)
from ownvitals.analysis.rules.episodes import (
    Episode,
    Origin,
    Step,
    Transition,
    TrendState,
    advance_persistent,
    advance_simple,
    reconcile,
)

D1 = date(2026, 3, 1)
RULES = ["B2", "B4", "B5", "C2"]


def day(n: int) -> date:
    """Day *n* of a scripted run (day 1 is D1)."""
    return D1 + timedelta(days=n - 1)


def true(n: int, rule: str = "B5") -> Finding:
    return fired(day(n), rule=rule, subject="x")


def false(n: int, rule: str = "B5") -> NoFinding:
    return NoFinding(rule, "x", day(n))


def short(n: int, rule: str = "B5") -> Insufficient:
    return Insufficient(rule, "x", day(n), "not enough")


def script(
    items: Sequence[RuleResult], *, today: date
) -> tuple[TrendState, list[tuple[int, Transition]]]:
    state = TrendState()
    seen: list[tuple[int, Transition]] = []
    for item in items:
        state, step = advance_persistent(state, item, today=today)
        if step.transition is not Transition.NONE:
            seen.append(((item.day - D1).days + 1, step.transition))
    return state, seen


def opened_on(items: Sequence[RuleResult], today: date = date(2026, 3, 22)) -> list[int]:
    return [n for n, t in script(items, today=today)[1] if t is Transition.OPENED]


# --- simple episodes --------------------------------------------------------------------------


def simple(rule: str, subject: str, on: date, *, start: int = 0) -> Finding:
    return fired(on, rule=rule, subject=subject, window_days=start or 3)


def test_consecutive_firings_update_one_episode_a_non_firing_ends_it_then_a_new_one_opens() -> None:
    today = day(30)
    first = fired(day(10), rule="C1", subject="heart_pulse", window_days=3)
    step = advance_simple(None, first, today=today)
    assert step.transition is Transition.OPENED
    assert step.episode is not None
    grown = fired(day(11), rule="C1", subject="heart_pulse", window_days=4)
    again = advance_simple(step.episode, grown, today=today)
    assert again.transition is Transition.UPDATED
    assert again.episode is not None
    assert (again.episode.window_end, again.episode.last_seen) == (day(11), day(11))
    assert again.episode.reference_day == step.episode.reference_day
    assert again.episode.first_seen == day(10)
    assert again.episode.origin is step.episode.origin
    ended = advance_simple(again.episode, NoFinding("C1", "heart_pulse", day(12)), today=today)
    assert (ended.transition, ended.episode, ended.ended) == (
        Transition.ENDED,
        None,
        again.episode,
    )
    reopened = advance_simple(
        ended.episode, fired(day(13), rule="C1", subject="heart_pulse"), today=today
    )
    assert reopened.transition is Transition.OPENED
    assert reopened.episode is not None
    assert reopened.episode.first_seen == day(13)


def test_insufficient_data_ends_a_simple_episode_too_and_nothing_to_end_is_none() -> None:
    episode = advance_simple(None, fired(day(5), rule="B1", subject="weight"), today=day(5)).episode
    gone = advance_simple(episode, Insufficient("B1", "weight", day(6), "x"), today=day(6))
    assert gone.transition is Transition.ENDED
    assert advance_simple(None, false(6), today=day(6)) == Step(None, Transition.NONE)


def test_a_backfill_episode_stays_backfill_and_a_live_one_stays_live() -> None:
    today = day(40)
    old = advance_simple(None, fired(day(30), rule="C1x", subject="heart_pulse"), today=today)
    assert old.episode is not None
    assert old.episode.origin is Origin.BACKFILL  # window ends 10 days back
    extended = advance_simple(
        old.episode, fired(day(40), rule="C1x", subject="heart_pulse"), today=today
    )
    assert extended.episode is not None
    assert extended.episode.origin is Origin.BACKFILL
    live = advance_simple(None, fired(day(40), rule="C1x", subject="heart_pulse"), today=today)
    assert live.episode is not None
    assert live.episode.origin is Origin.LIVE
    later = advance_simple(
        live.episode, fired(day(70), rule="C1x", subject="heart_pulse"), today=day(70)
    )
    assert later.episode is not None
    assert later.episode.origin is Origin.LIVE  # 30 days later, still live


def test_only_the_first_write_of_a_live_episode_notifies() -> None:
    live = advance_simple(None, fired(day(40), rule="B1", subject="weight"), today=day(40))
    assert live.notify
    assert not advance_simple(
        live.episode, fired(day(41), rule="B1", subject="weight"), today=day(41)
    ).notify
    old = advance_simple(None, fired(day(10), rule="B1", subject="weight"), today=day(40))
    assert not old.notify
    assert not advance_simple(old.episode, false(11), today=day(41)).notify


def test_a_recompute_with_the_same_reference_day_keeps_first_seen_and_origin() -> None:
    previous = advance_simple(
        None, fired(day(10), rule="B1", subject="weight"), today=day(11)
    ).episode
    assert previous is not None
    recomputed = advance_simple(
        None, fired(day(10), rule="B1", subject="weight"), today=day(90)
    ).episode
    assert recomputed is not None
    assert recomputed.origin is Origin.BACKFILL
    kept = reconcile(previous, recomputed)
    assert (kept.first_seen, kept.origin) == (previous.first_seen, Origin.LIVE)
    other = advance_simple(None, fired(day(12), rule="B1", subject="weight"), today=day(90)).episode
    assert other is not None
    assert reconcile(previous, other) == other
    assert reconcile(None, other) == other


# --- persistence ------------------------------------------------------------------------------


@pytest.mark.parametrize("rule", RULES)
def test_daily_data_opens_on_the_21st_calendar_day_and_not_on_the_20th(rule: str) -> None:
    assert opened_on([true(n, rule) for n in range(1, 21)]) == []
    state, seen = script([true(n, rule) for n in range(1, 22)], today=day(21))
    assert seen == [(21, Transition.OPENED)]
    episode = state.episode
    assert episode is not None
    assert episode.window_start == day(1) - timedelta(days=89)  # day 1's window start
    assert (episode.reference_day, episode.window_end, episode.origin) == (
        day(1),
        day(21),
        Origin.LIVE,
    )


def test_the_origin_of_a_persistent_episode_is_that_of_the_day_it_opens() -> None:
    state, _ = script([true(n) for n in range(1, 22)], today=day(40))
    assert state.episode is not None
    assert state.episode.origin is Origin.BACKFILL


def test_a_false_day_restarts_the_run_so_nothing_opens_until_day_42() -> None:
    items: list[RuleResult] = [*(true(n) for n in range(1, 21)), false(21)]
    items += [true(n) for n in range(22, 43)]
    assert opened_on(items[:-1]) == []  # through day 41
    assert opened_on(items) == [42]


def test_weekly_evaluations_need_six_of_them() -> None:
    weekly = [1, 8, 15, 22, 29, 36]
    assert opened_on([true(n) for n in weekly[:4]]) == []  # day 22: 22 days, 4 evaluations
    assert opened_on([true(n) for n in weekly[:5]]) == []  # day 29: 5
    assert opened_on([true(n) for n in weekly]) == [36]


def test_six_evaluations_spanning_21_days_open_and_spanning_20_days_do_not() -> None:
    assert opened_on([true(n) for n in (1, 3, 5, 7, 9, 21)]) == [21]
    early = [true(n) for n in (1, 3, 5, 7, 9, 20)]
    assert opened_on(early) == []
    assert opened_on([*early, true(21)]) == [21]  # a true evaluation on day 21 or later
    assert opened_on([*early, true(25)]) == [25]


def test_insufficient_data_breaks_the_run_and_the_next_true_evaluation_starts_a_new_one() -> None:
    items: list[RuleResult] = [*(true(n) for n in range(1, 11)), short(11)]
    items += [true(n) for n in range(12, 33)]  # 21 days from day 12
    assert opened_on(items[:-1]) == []
    assert opened_on(items) == [32]


def opened(rule: str = "B5") -> TrendState:
    return script([true(n, rule) for n in range(1, 22)], today=day(21))[0]  # open at day 21


def fail_days(state: TrendState, days: range, make=false) -> list[Transition]:
    seen: list[Transition] = []
    for n in days:
        state, step = advance_persistent(state, make(n), today=day(n))
        seen.append(step.transition)
    return seen


def test_an_open_episode_survives_five_failing_days_and_ends_on_the_seventh_day() -> None:
    # e = 22: it survives 22 … 27 (six days) and ends on day 28 = e + 6
    transitions = fail_days(opened(), range(22, 29))
    assert transitions[:-1] == [Transition.NONE] * 6
    assert transitions[-1] is Transition.ENDED


def test_one_true_evaluation_in_the_window_keeps_it_open() -> None:
    state = opened()
    for n in range(22, 27):
        state, _ = advance_persistent(state, false(n), today=day(n))
    state, step = advance_persistent(state, true(27), today=day(27))
    assert step.transition is Transition.UPDATED
    assert fail_days(state, range(28, 34)) == [Transition.NONE] * 6
    assert fail_days(state, range(28, 35))[-1] is Transition.ENDED


def test_insufficient_data_counts_as_a_failing_evaluation_of_an_open_episode() -> None:
    assert fail_days(opened(), range(22, 29), short)[-1] is Transition.ENDED


def test_days_without_an_evaluation_do_not_end_an_episode_and_a_new_one_needs_a_new_run() -> None:
    state = opened()  # nothing is evaluated for a month: nothing changes
    assert state.episode is not None
    after, step = advance_persistent(state, true(60), today=day(60))
    assert step.transition is Transition.UPDATED
    ended, step = advance_persistent(after, false(68), today=day(68))
    assert step.transition is Transition.ENDED
    # a true evaluation right after the end starts a run again: nothing opens until day 21 later
    fresh, step = advance_persistent(ended, true(69), today=day(69))
    assert (step.transition, fresh.episode) == (Transition.NONE, None)
    assert fresh.run_evaluations == 1


def test_an_updated_persistent_episode_keeps_its_reference_day_and_origin() -> None:
    state = opened()
    first = state.episode
    state, step = advance_persistent(state, true(22), today=day(22))
    assert step.episode is not None
    assert first is not None
    assert (step.episode.reference_day, step.episode.origin, step.episode.first_seen) == (
        first.reference_day,
        first.origin,
        first.first_seen,
    )
    assert step.episode.window_end == day(22)
    assert not step.notify


def test_other_rules_open_on_the_first_firing_and_end_on_the_first_non_firing_evaluation() -> None:
    for rule in ("C1", "C1x", "B1", "B3", "B6"):
        step = advance_simple(None, fired(day(1), rule=rule, subject="x"), today=day(1))
        assert step.transition is Transition.OPENED
        assert (
            advance_simple(step.episode, false(2, rule), today=day(2)).transition
            is Transition.ENDED
        )


def test_the_episode_type_carries_the_latest_finding() -> None:
    episode: Episode | None = advance_simple(
        None, fired(day(1), rule="B1", subject="weight"), today=day(1)
    ).episode
    assert episode is not None
    assert episode.latest.rule_id == "B1"
