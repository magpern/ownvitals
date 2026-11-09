"""The notification hold's pure decision (``ownvitals.notify.hold``; ADR 0006 section 11 and its
test specification, #195). Synthetic ids and times only; no database, no clock.

The same cases on a real database, through the read function and the step, are
``test_notify_hold_db.py``.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from ownvitals.config_notify import NotifySettings
from ownvitals.notify.hold import (
    DEFAULT_BOUND,
    EcgFacts,
    EcgStream,
    EcgSync,
    Hold,
    decide,
    end_reason,
    group_state,
    held,
    new_revision,
)
from ownvitals.notify.tables import REASONS
from ownvitals.views.findings_models import TriggerLink

SYNTHETIC = True
T0 = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
C = UUID(int=1)  # a synthetic connection id
HOLD = Hold(T0, T0, T0, (91,))


def link(grpid: int = 91, *, linked: int = 0, without: int = 0, connection: UUID | None = C):
    return TriggerLink(grpid, connection, linked, without)


def stream(capability: str | None = "supported", complete: bool = True, ok: datetime | None = None):
    return EcgStream(capability, complete, ok)


def run(*links: TriggerLink, facts: EcgFacts | None = None, now: datetime = T0, **kw: object):
    args: dict[str, object] = {
        "rule_id": "E6",
        "hidden": False,
        "not_evaluated": False,
        "links": links,
        "facts": facts or EcgFacts({C: stream()}),
        "hold": HOLD,
        "now": now,
    }
    return decide(**(args | kw))  # pyright: ignore[reportArgumentType]


# --- which rules are held ----------------------------------------------------------------------


@pytest.mark.parametrize("rule", ["E1", "E2", "E3", "B1", "C1", "Q1"])
def test_only_e4_and_e6_are_held_and_the_rest_send_at_once(rule: str) -> None:
    assert not held(rule)
    decision = run(link(), rule_id=rule)  # an unlinked trigger would hold E4 or E6
    assert (decision.wait, decision.reason, decision.outcome) == (False, None, "sent")


@pytest.mark.parametrize("rule", ["E4", "E6"])
def test_e4_and_e6_wait_for_an_unlinked_trigger(rule: str) -> None:
    assert held(rule)
    assert run(link(), rule_id=rule).wait


# --- the gate result ends the hold -------------------------------------------------------------


def test_a_linked_recording_with_a_current_estimated_ends_it_linked() -> None:
    decision = run(link(linked=1))
    assert (decision.wait, decision.reason, decision.outcome) == (False, "linked", "sent")


def test_a_failure_ends_it_hidden_and_no_result_ends_it_not_evaluated() -> None:
    assert run(link(linked=1), hidden=True).outcome == "hidden"
    assert run(link(linked=1), not_evaluated=True).outcome == "sent_not_evaluated"


def test_a_linked_recording_without_a_current_estimated_waits() -> None:
    assert run(link(linked=2, without=1)).wait
    assert run(link(linked=1), link(92, linked=1, without=1)).wait


def test_only_the_trigger_holds() -> None:
    """A baseline recording is not a trigger link at all: the read function gives only the
    trigger groups, so one linked trigger with a current estimate ends the hold."""
    assert not run(link(linked=1)).wait


# --- no link possible --------------------------------------------------------------------------


@pytest.mark.parametrize("capability", ["unsupported", "not_authorized", "not_available"])
def test_a_capability_without_recordings_ends_it_at_once(capability: str) -> None:
    facts = EcgFacts({C: stream(capability)})
    decision = run(link(), facts=facts, not_evaluated=True)
    assert (decision.wait, decision.reason, decision.outcome) == (
        False,
        "capability_unsupported",
        "sent_not_evaluated",
    )


@pytest.mark.parametrize("capability", ["supported", "unknown", None])
def test_a_capability_that_can_list_recordings_waits(capability: str | None) -> None:
    assert run(link(), facts=EcgFacts({C: stream(capability)})).wait


def sync_facts(started: datetime, ok: datetime | None, complete: bool = True) -> EcgFacts:
    return EcgFacts({C: stream(ok=ok, complete=complete)}, (EcgSync(C, started),))


def test_a_complete_sync_that_started_after_the_revision_ends_it() -> None:
    later = T0 + timedelta(minutes=5)
    decision = run(link(), facts=sync_facts(later, later + timedelta(minutes=1)))
    assert (decision.wait, decision.reason) == (False, "ecg_sync_no_match")


def test_a_sync_that_started_before_or_completed_only_partly_does_not_end_it() -> None:
    before = T0 - timedelta(minutes=5)
    after = T0 + timedelta(minutes=5)
    assert run(link(), facts=sync_facts(before, after)).wait  # started before the revision
    assert run(link(), facts=sync_facts(after, None)).wait  # never stored a success
    assert run(link(), facts=sync_facts(after, after - timedelta(seconds=1))).wait  # an older one
    assert run(link(), facts=sync_facts(after, after, complete=False)).wait  # partly
    other = EcgFacts({C: stream(ok=after)}, (EcgSync(uuid4(), after),))
    assert run(link(), facts=other).wait  # another connection's sync


def test_a_group_with_no_live_measure_group_cannot_be_linked() -> None:
    decision = run(link(connection=None))
    assert (decision.wait, decision.reason) == (False, "no_trigger")
    assert run().reason == "no_trigger"  # no trigger group at all


def test_signal_missing_alone_ends_it_unless_another_group_can_still_be_linked() -> None:
    """A recording kept with ``signal`` NULL has, after its recompute, a current ``estimated``
    without a gate result: it holds nothing. Another trigger group that can still be linked
    does."""
    alone = run(link(linked=1), link(92, connection=None), not_evaluated=True)
    assert (alone.wait, alone.reason, alone.outcome) == (False, "linked", "sent_not_evaluated")
    assert run(link(linked=1), link(92)).wait


def test_the_end_reason_prefers_a_link_then_the_capability_then_the_sync() -> None:
    assert end_reason(["sync", "capability", "linked"]) == "linked"
    assert end_reason(["sync", "capability"]) == "capability_unsupported"
    assert end_reason(["missing", "sync"]) == "ecg_sync_no_match"
    assert end_reason(["missing"]) == "no_trigger"
    assert end_reason([]) == "no_trigger"
    assert set(REASONS) == {
        "linked",
        "capability_unsupported",
        "ecg_sync_no_match",
        "no_trigger",
        "timeout",
        "withdrawn",
    }


def test_group_state_reads_each_group_alone() -> None:
    facts = EcgFacts({C: stream("unsupported")})
    assert group_state(link(linked=1), facts, T0) == "linked"
    assert group_state(link(linked=1, without=1), facts, T0) == "waiting"
    assert group_state(link(), facts, T0) == "capability"
    assert group_state(link(connection=None), facts, T0) == "missing"


# --- the bound ---------------------------------------------------------------------------------


def test_just_before_the_bound_it_waits_and_at_the_bound_it_ends_timeout() -> None:
    assert run(link(), now=T0 + DEFAULT_BOUND - timedelta(seconds=1)).wait
    decision = run(link(), now=T0 + DEFAULT_BOUND, not_evaluated=True)
    assert (decision.wait, decision.reason, decision.outcome) == (
        False,
        "timeout",
        "sent_not_evaluated",
    )


def test_the_bound_is_a_setting_and_at_most_24_hours() -> None:
    bound = NotifySettings(notify_hold_hours=2).hold_bound
    assert bound == timedelta(hours=2)
    assert run(link(), now=T0 + bound - timedelta(seconds=1), bound=bound).wait
    assert run(link(), now=T0 + bound, bound=bound).reason == "timeout"
    assert NotifySettings().hold_bound == DEFAULT_BOUND == timedelta(hours=24)
    for wrong in (0, -1, 25):
        with pytest.raises(ValidationError):
            NotifySettings(notify_hold_hours=wrong)


def test_the_bound_counts_from_the_first_revision_and_the_sync_from_the_current_one() -> None:
    """A new revision at T0 + 1 h moves ``revision_at`` only: a sync that started between the
    two revisions no longer ends the hold, and the bound still ends it at T0 + 24 h."""
    restarted = Hold(T0, T0 + timedelta(hours=1), T0, (91, 92))
    between = sync_facts(T0 + timedelta(minutes=30), T0 + timedelta(minutes=31))
    assert not run(link(), facts=between).wait  # the first revision: it ends
    assert run(link(), facts=between, hold=restarted, now=T0 + timedelta(hours=2)).wait
    late = run(link(), facts=between, hold=restarted, now=T0 + DEFAULT_BOUND)
    assert late.reason == "timeout"


def test_a_new_revision_is_a_new_last_seen_or_a_new_set_of_trigger_groups() -> None:
    assert not new_revision(HOLD, T0, [91])
    assert not new_revision(HOLD, T0, [91, 91])
    assert new_revision(HOLD, T0 + timedelta(days=1), [91])
    assert new_revision(HOLD, T0, [91, 92])
