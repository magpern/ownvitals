"""The typed results and query of the findings read function (ADR 0006 section 11): what a
caller passes and gets back, and the constants that define them. Pure: no database, no session.

Split from ``views.findings_read`` (no behaviour change; the reads stay there, behind the one
guard-listed read function). A ``repr`` here names no value.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Literal, get_args
from uuid import UUID

from ownvitals.analysis.rules.core import SPECS, Tier
from ownvitals.ecg.overlay import EstimateView, Outcome

Consumer = Literal["dashboard", "notification", "export", "doctor_pdf", "ai_context"]
CONSUMERS: Final = get_args(Consumer)
# Section 11: the export carries every finding with its overlay state; the others leave a hidden
# finding out.
INCLUDES_HIDDEN: Final = frozenset({"export"})
Tab = Literal["live", "history"]
TABS: Final = get_args(Tab)
FINDING_TIERS: Final = ("device", "trend", "experimental", "system")
EXPERIMENTAL: Final = "experimental"
# The design's area filter (Findings mock-up) by the rule's letter; Q1 is the system area.
AREAS: Final = ("body", "heart", "nerve", "activity", "system")
AREA_BY_LETTER: Final = {
    "B": "body",
    "C": "heart",
    "E": "heart",
    "N": "nerve",
    "A": "activity",
    "Q": "system",
}
# Experimental by definition, whatever a stored row's tier says: ADR 0010 §7's experimental
# rules and ADR 0006 section 11's E4 and E5.
EXPERIMENTAL_RULES: Final = frozenset(
    {rule for rule, spec in SPECS.items() if spec.tier is Tier.EXPERIMENTAL} | {"E4", "E5"}
)
# The API's page size (``GET /api/findings``): default and maximum. Other consumers read all.
PAGE_DEFAULT: Final = 50
PAGE_MAX: Final = 200


@dataclass(frozen=True)
class FindingsQuery:
    """The list filters: ``tab`` (``live``/``history``), ``tier`` and ``area``; ``None`` is all.
    ``limit`` (``None``: every row) and ``offset`` page over the stored rows, newest
    ``last_seen`` first; a page can hold fewer than ``limit`` findings when some are hidden."""

    tab: Tab | None = None
    tier: str | None = None
    area: str | None = None
    limit: int | None = None
    offset: int = 0
    notified: bool = False  # only findings whose notification was sent (the in-app list)

    def __post_init__(self) -> None:
        if self.tab is not None and self.tab not in TABS:
            raise ValueError("unknown tab")
        if self.tier is not None and self.tier not in FINDING_TIERS:
            raise ValueError("unknown tier")
        if self.area is not None and self.area not in AREAS:
            raise ValueError("unknown area")
        if self.limit is not None and not 1 <= self.limit <= PAGE_MAX:
            raise ValueError("limit out of range")
        if self.offset < 0:
            raise ValueError("offset out of range")


@dataclass(frozen=True)
class GateView:
    """Our gate on the finding's trigger recordings; given only behind the switch."""

    outcome: Outcome
    counted: int
    failed: int


@dataclass(frozen=True)
class FindingView:
    """One finding as an output may show it. ``hidden`` is set only for the export (the other
    consumers never receive a hidden finding). ``not_evaluated`` is section 11's mark on E4 and
    E6. ``gate`` is ``None`` without the switch."""

    id: UUID
    rule_id: str
    rule_version: str
    tier: str
    area: str | None
    severity: str
    confidence: str
    data_quality: Mapping[str, Any]
    origin: str
    metric_keys: tuple[str, ...]
    subject: str
    window_start: date
    window_end: date
    reference_day: date | None
    evidence: Mapping[str, Any]
    status: str
    first_seen: datetime
    last_seen: datetime
    ended_at: datetime | None
    notify_state: str
    notified_at: datetime | None
    seen_at: datetime | None
    hidden: bool
    not_evaluated: bool
    gate: GateView | None

    def __repr__(self) -> str:
        return "FindingView redacted"

    @property
    def trigger_groups(self) -> tuple[int, ...]:
        return groups_of_evidence(self.evidence)


@dataclass(frozen=True)
class Reading:
    """A reading that drove a finding: one of its trigger groups' observations."""

    metric_key: str
    ts: datetime
    value: Decimal
    grpid: int | None
    quality_flags: tuple[str, ...]

    def __repr__(self) -> str:
        return "Reading redacted"


@dataclass(frozen=True)
class LinkedEstimate:
    recording_id: UUID
    grpid: int
    view: EstimateView

    def __repr__(self) -> str:
        return "LinkedEstimate redacted"


@dataclass(frozen=True)
class FindingDetail:
    finding: FindingView
    readings: tuple[Reading, ...]
    estimates: tuple[LinkedEstimate, ...]  # empty without the switch

    def __repr__(self) -> str:
        return "FindingDetail redacted"


@dataclass(frozen=True)
class TriggerLink:
    """One trigger group of a finding, as ADR 0006 section 11's notification hold needs it: the
    connection of its live measure group (``None``: no live group has that grpid), how many
    recordings link to it now (A7) and how many of those have no current ``estimated``."""

    grpid: int
    connection: UUID | None
    linked: int
    without_estimate: int


@dataclass(frozen=True)
class NotificationCandidate:
    """A finding for the notification step, hidden or not (the step never sends a hidden one;
    it ends its hold), with its trigger groups' links."""

    finding: FindingView
    links: tuple[TriggerLink, ...]

    def __repr__(self) -> str:
        return "NotificationCandidate redacted"


def area_of(rule_id: str) -> str | None:
    return AREA_BY_LETTER.get(rule_id[:1]) if rule_id else None


def groups_of_evidence(evidence: Mapping[str, Any]) -> tuple[int, ...]:
    raw = evidence.get("trigger_groups")
    if not isinstance(raw, list):
        return ()
    found: list[int] = []
    for value in raw:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, int) and not isinstance(value, bool):
            found.append(value)
    return tuple(dict.fromkeys(found))
