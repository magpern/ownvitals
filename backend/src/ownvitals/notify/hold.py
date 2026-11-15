"""The notification hold of ADR 0006 section 11 (#195). Pure: no database, no clock (``now`` is
passed in).

**E4 and E6 notifications wait for the gate.** One is held while a trigger group can still be
linked or a linked recording lacks a current ``estimated``, for at most ``bound`` after the
finding's first revision. Per trigger group (only the trigger counts; a baseline recording never
holds a notification):

- **linked**: at least one recording links to it (A7). It waits while any of them has no current
  ``estimated`` (a ``signal_missing`` one is current: it has no gate result, and holds nothing).
- **no link possible**: the connection's ``ecg`` capability is ``unsupported``,
  ``not_authorized`` or ``not_available`` (PLAN §4.2), or an ``ecg`` sync that **started after
  the current revision began** completed successfully without a match (A9). A sync that started
  before, failed or completed only partly does not count: the stream's cursor must be complete
  and its last success no earlier than the sync's start. The revision start is no earlier than
  the group's storage (the finding is computed from the stored group), so a sync counted here
  started after the group was stored; one that started between the two is not counted, which
  can only hold longer, never shorter.
- **missing**: no live measure group has the grpid (deleted): it can never be linked.
- otherwise it **can still be linked**, and the finding waits.

The hold ends when nothing waits, or at the bound (``timeout``). The overlay then decides once:
hidden sends nothing, ever; shown sends, marked "not evaluated by ownvitals" when no trigger
recording has a gate result. A new revision restarts the hold for that revision (``revision_at``
moves; ``started_at``, and so the bound, stays). E1 to E3, and every rule the overlay does not
gate, are sent at once and never held.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from ownvitals.ecg.overlay import GATED_RULES
from ownvitals.notify.tables import HoldOutcome, Reason
from ownvitals.views.findings_models import TriggerLink

# PLAN §4.2: the capability states in which no recording of the stream can be listed.
NO_LINK_CAPABILITIES: Final = frozenset({"unsupported", "not_authorized", "not_available"})
DEFAULT_BOUND: Final = timedelta(hours=24)

GroupState = Literal["linked", "waiting", "capability", "sync", "missing"]


@dataclass(frozen=True)
class EcgStream:
    """One connection's ``ecg`` cursor: its capability, whether it is complete, and when it last
    stored a successful pass."""

    capability: str | None
    complete: bool
    last_ok_at: datetime | None


@dataclass(frozen=True)
class EcgSync:
    """One completed ``ecg`` sync job of a connection, by its start."""

    connection: UUID
    started_at: datetime


@dataclass(frozen=True)
class EcgFacts:
    streams: Mapping[UUID, EcgStream] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    syncs: tuple[EcgSync, ...] = ()

    def synced_since(self, connection: UUID, since: datetime) -> bool:
        """A successful, complete ``ecg`` sync of ``connection`` that started at or after
        ``since``."""
        stream = self.streams.get(connection)
        if stream is None or not stream.complete or stream.last_ok_at is None:
            return False
        last_ok = stream.last_ok_at
        return any(
            s.connection == connection and s.started_at >= since and last_ok >= s.started_at
            for s in self.syncs
        )


@dataclass(frozen=True)
class Hold:
    """A stored hold: the first revision's start, the current revision's start, and what tells
    that revision apart (its ``last_seen`` and trigger groups)."""

    started_at: datetime
    revision_at: datetime
    revision_seen: datetime
    revision_groups: tuple[int, ...]


@dataclass(frozen=True)
class Decision:
    """``wait``: keep holding. Otherwise the hold (if any) ends with ``reason`` and the finding
    is sent (``sent``, ``sent_not_evaluated``) or not (``hidden``)."""

    wait: bool
    reason: Reason | None = None
    outcome: HoldOutcome | None = None


WAIT: Final = Decision(wait=True)


def held(rule_id: str) -> bool:
    """Whether a finding of ``rule_id`` waits for the gate (E4 and E6)."""
    return rule_id in GATED_RULES


def outcome_of(hidden: bool, not_evaluated: bool) -> HoldOutcome:
    if hidden:
        return "hidden"
    return "sent_not_evaluated" if not_evaluated else "sent"


def new_revision(hold: Hold, last_seen: datetime, groups: Iterable[int]) -> bool:
    """Whether the finding has moved to another revision since the hold's current one."""
    return hold.revision_seen != last_seen or hold.revision_groups != tuple(sorted(set(groups)))


def group_state(link: TriggerLink, facts: EcgFacts, since: datetime) -> GroupState:
    if link.linked:
        return "waiting" if link.without_estimate else "linked"
    if link.connection is None:
        return "missing"
    stream = facts.streams.get(link.connection)
    if stream is not None and stream.capability in NO_LINK_CAPABILITIES:
        return "capability"
    if facts.synced_since(link.connection, since):
        return "sync"
    return "waiting"


def end_reason(states: Sequence[GroupState]) -> Reason:
    """The reason of an end with nothing waiting: a link first, then why no link is possible."""
    if "linked" in states:
        return "linked"
    if "capability" in states:
        return "capability_unsupported"
    if "sync" in states:
        return "ecg_sync_no_match"
    return "no_trigger"


def decide(
    *,
    rule_id: str,
    hidden: bool,
    not_evaluated: bool,
    links: Sequence[TriggerLink],
    facts: EcgFacts,
    hold: Hold,
    now: datetime,
    bound: timedelta = DEFAULT_BOUND,
) -> Decision:
    """Whether the held finding of ``rule_id`` keeps waiting at ``now``, or how its hold ends.
    ``hidden`` and ``not_evaluated`` are the overlay's at this read."""
    if not held(rule_id):
        return Decision(wait=False, reason=None, outcome=outcome_of(hidden, not_evaluated))
    states: list[GroupState] = [group_state(link, facts, hold.revision_at) for link in links]
    if "waiting" in states:
        if now - hold.started_at < bound:
            return WAIT
        reason: Reason = "timeout"
    else:
        reason = end_reason(states)
    return Decision(wait=False, reason=reason, outcome=outcome_of(hidden, not_evaluated))


__all__ = [
    "DEFAULT_BOUND",
    "NO_LINK_CAPABILITIES",
    "WAIT",
    "Decision",
    "EcgFacts",
    "EcgStream",
    "EcgSync",
    "Hold",
    "decide",
    "end_reason",
    "group_state",
    "held",
    "new_revision",
    "outcome_of",
]
