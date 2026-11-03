"""Origin and episode state transitions of the v1 finding rules (ADR 0010 §4). Pure.

* ``origin(window_end, today)``: ``live`` or ``backfill``;
* ``advance_simple``: open on the first firing, update while firing, end on the first
  non-firing (or insufficient) evaluation;
* ``advance_persistent``: trend-test rules (B2, B4, B5, C2, N3): 21 calendar days and at least
  6 evaluations to open, 7 calendar days of failing evaluations to end;
* ``reconcile``: a recompute with the same rule, subject and reference day keeps ``first_seen``
  and ``origin``.

Storage (the ``findings`` columns, the active-episode uniqueness) is the wiring issue's.
"""

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from ownvitals.analysis.rules.core import Finding, RuleResult

LIVE_DAYS = 6  # a window ending within the last 7 days, today included, is live
PERSIST_DAYS = 21  # calendar days a trend-test run must span to open an episode
PERSIST_EVALUATIONS = 6
END_FAIL_DAYS = 7  # calendar days of failing evaluations that end an open episode


class Origin(StrEnum):
    LIVE = "live"
    BACKFILL = "backfill"


def origin(window_end: date, today: date) -> Origin:
    """``live`` when the window ends in the last 7 days (today included, or later: a zone ahead)."""
    return Origin.LIVE if (today - window_end).days <= LIVE_DAYS else Origin.BACKFILL


class Transition(StrEnum):
    NONE = "none"
    OPENED = "opened"
    UPDATED = "updated"
    ENDED = "ended"


@dataclass(frozen=True, slots=True)
class Episode:
    """An active episode of one (rule, subject). ``reference_day``, ``first_seen`` and ``origin``
    never change; ``latest`` is the latest firing's proposal (evidence, confidence, quality)."""

    rule_id: str
    subject: str
    reference_day: date
    window_start: date
    window_end: date
    first_seen: date
    last_seen: date
    origin: Origin
    latest: Finding


@dataclass(frozen=True, slots=True)
class Step:
    """The result of one transition. ``episode`` is the active episode afterwards (``None`` once
    ended); ``ended`` is the one that ended. Only an ``OPENED`` live episode notifies."""

    episode: Episode | None
    transition: Transition
    ended: Episode | None = None

    @property
    def notify(self) -> bool:
        return (
            self.transition is Transition.OPENED
            and self.episode is not None
            and self.episode.origin is Origin.LIVE
        )


def _opened(finding: Finding, today: date, *, start: date, reference: date) -> Episode:
    return Episode(
        rule_id=finding.rule_id,
        subject=finding.subject,
        reference_day=reference,
        window_start=start,
        window_end=finding.window_end,
        first_seen=finding.day,
        last_seen=finding.day,
        origin=origin(finding.window_end, today),
        latest=finding,
    )


def _extended(episode: Episode, finding: Finding) -> Episode:
    return replace(
        episode,
        window_end=max(episode.window_end, finding.window_end),
        last_seen=finding.day,
        latest=finding,
    )


def advance_simple(episode: Episode | None, result: RuleResult, *, today: date) -> Step:
    """Rules that are not trend tests: the first firing opens an episode, a firing while one is
    active updates it (the window grows; reference day and origin stay), and the first
    evaluation that does not fire, or has insufficient data, ends it."""
    if isinstance(result, Finding):
        if episode is None:
            opened = _opened(
                result, today, start=result.window_start, reference=result.reference_day
            )
            return Step(opened, Transition.OPENED)
        return Step(_extended(episode, result), Transition.UPDATED)
    if episode is None:
        return Step(None, Transition.NONE)
    return Step(None, Transition.ENDED, ended=episode)


@dataclass(frozen=True, slots=True)
class TrendState:
    """The persistence state of a trend-test rule and subject: the current run of true
    evaluations (``run_*``) and the open episode with the day of its last true evaluation."""

    run_first_day: date | None = None
    run_window_start: date | None = None
    run_evaluations: int = 0
    last_true_day: date | None = None
    episode: Episode | None = None


def advance_persistent(
    state: TrendState, result: RuleResult, *, today: date
) -> tuple[TrendState, Step]:
    """Persistence of trend-test rules (§4). The condition is a ``Finding`` of the evaluation.

    An episode opens at a true evaluation when every evaluation since the run's first one was
    true, the run spans at least 21 calendar days (first and last included) and holds at least 6
    evaluations. A false or insufficient evaluation breaks a run. An open episode ends at a failing
    evaluation when every evaluation in the 7 calendar days ending on it failed (the window holds
    at least this one), i.e. its last true evaluation was 7 or more days earlier; one true
    evaluation in the window keeps it open. Days without an evaluation neither count nor break
    anything: call this only for evaluation days, in order."""
    if isinstance(result, Finding):
        if state.episode is not None:
            updated = _extended(state.episode, result)
            return (
                TrendState(last_true_day=result.day, episode=updated),
                Step(updated, Transition.UPDATED),
            )
        first = state.run_first_day or result.day
        start = state.run_window_start or result.window_start
        count = state.run_evaluations + 1
        if (result.day - first).days + 1 >= PERSIST_DAYS and count >= PERSIST_EVALUATIONS:
            opened = _opened(result, today, start=start, reference=first)
            return (
                TrendState(last_true_day=result.day, episode=opened),
                Step(opened, Transition.OPENED),
            )
        return TrendState(first, start, count), Step(None, Transition.NONE)
    if state.episode is None:
        return TrendState(), Step(None, Transition.NONE)  # the run is broken
    last_true = state.last_true_day or state.episode.last_seen
    if (result.day - last_true).days >= END_FAIL_DAYS:
        return TrendState(), Step(None, Transition.ENDED, ended=state.episode)
    return state, Step(state.episode, Transition.NONE)


def reconcile(previous: Episode | None, recomputed: Episode) -> Episode:
    """A recompute after a data revision (PLAN §5): the same rule, subject and reference day keeps
    its ``first_seen`` and origin; another reference day is a new episode."""
    if (
        previous is not None
        and previous.rule_id == recomputed.rule_id
        and previous.subject == recomputed.subject
        and previous.reference_day == recomputed.reference_day
    ):
        return replace(recomputed, first_seen=previous.first_seen, origin=previous.origin)
    return recomputed
