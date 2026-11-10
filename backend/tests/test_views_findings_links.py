"""``trigger_links`` (``ownvitals.views.findings_links``) with a grpid stored under two
connections, and the hold decision on top of it. Pure; synthetic ids and times only."""

from datetime import UTC, datetime
from uuid import UUID

from ownvitals.ecg.overlay import GroupKey, RecordingKey
from ownvitals.notify.hold import EcgFacts, EcgStream, Hold, decide
from ownvitals.views.findings_links import trigger_links

SYNTHETIC = True
T0 = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
A, B = UUID(int=1), UUID(int=2)


def group(grpid: int, connection: UUID) -> GroupKey:
    return GroupKey(grpid, connection, T0, frozenset({"synthetic_device"}), 91, frozenset())


def recording(connection: UUID, n: int = 1) -> RecordingKey:
    return RecordingKey(UUID(int=100 + n), connection, T0, "synthetic_device", 91, "fp", None)


def decision(links, facts: EcgFacts):
    return decide(
        rule_id="E6",
        hidden=False,
        not_evaluated=False,
        links=links,
        facts=facts,
        hold=Hold(T0, T0, T0, (7,)),
        now=T0,
    )


def test_a_grpid_under_two_connections_gives_one_link_per_connection() -> None:
    links = trigger_links({7: (recording(B),)}, [group(7, A), group(7, B)])
    assert [(k.connection, k.linked) for k in links] == [(A, 0), (B, 1)]


def test_a_grpid_with_no_live_group_has_one_link_without_a_connection() -> None:
    links = trigger_links({7: ()}, [])
    assert [(k.connection, k.linked) for k in links] == [(None, 0)]


def test_one_unsupported_and_one_linkable_connection_keeps_the_hold() -> None:
    links = trigger_links({7: ()}, [group(7, A), group(7, B)])
    facts = EcgFacts(
        {A: EcgStream("unsupported", True, None), B: EcgStream("supported", True, None)}
    )
    assert decision(links, facts).wait is True


def test_a_recording_linked_on_the_other_connection_still_waits_for_its_estimate() -> None:
    links = trigger_links({7: (recording(B),)}, [group(7, A), group(7, B)])
    facts = EcgFacts(
        {A: EcgStream("unsupported", True, None), B: EcgStream("supported", True, None)}
    )
    assert decision(links, facts).wait is True


def test_both_connections_unsupported_ends_it_capability_unsupported() -> None:
    links = trigger_links({7: ()}, [group(7, A), group(7, B)])
    facts = EcgFacts(
        {A: EcgStream("unsupported", True, None), B: EcgStream("unsupported", True, None)}
    )
    assert decision(links, facts).reason == "capability_unsupported"
