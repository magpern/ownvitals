"""The one read function for findings and ECG estimates (ADR 0006 sections 9 and 11; ADR 0010;
PLAN §7.2 and D9).

Every output that shows a finding or an ``estimated`` reads it here: the dashboard and API
(``views.findings_routes``), notifications, the JSON/CSV export, the doctor PDF and the AI
context. Nothing else reads ``findings`` or ``estimated`` for presentation;
``tests/test_findings_read_guard.py`` fails for any other module that does, and names the few
that may (the rules engine's writes, the ``estimated`` recompute, the purge path and ``ecg
validate``).

One call is one ``user_session``: forced RLS on every table read, and every statement also
filters ``owner_id`` itself (ADR 0002). Then, in order:

1. **The experimental tier** (D9, ADR 0010) is read only when the owner-only switch
   ``experimental_findings`` is on (``flags.effective``: the environment's switch and the stored
   flag; default off). Without it no experimental finding is loaded, and a stored row whose
   ``rule_id`` is an experimental rule is dropped even if its tier says otherwise. Until the M8
   gate every account is a household account, so the switch is the whole rule.
2. **The overlay** (``ownvitals.ecg.overlay``): the trigger groups in the finding's evidence are
   linked (A7) to recordings now, and ``ecg_gate_overlay`` decides. E4 and E6 are hidden when at
   least half of the counted trigger recordings failed our gate, and carry "not evaluated by
   ownvitals" when none has a gate result. E1 to E3 are never hidden; their gate outcome, like
   every gate outcome and count, is given only behind the switch.
3. **The consumer**: a hidden finding is left out for every consumer but ``export``, which gets
   every finding with its overlay state (the user's complete data).
4. **Estimates** (section 9) only behind the switch, and only up to date
   (``overlay.estimate_view``).

The result is typed; its ``repr`` names no value.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, Literal, get_args
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.analysis.rules.core import SPECS, Tier
from ownvitals.config import Settings
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.ecg.overlay import (
    GATED_RULES,
    EstimateView,
    GroupKey,
    Outcome,
    Overlay,
    RecordingKey,
    ecg_gate_overlay,
    estimate_view,
    group_device_ids,
    recording_device_id,
    resolve_links,
)
from ownvitals.ecg.validate_cli import input_fingerprint
from ownvitals.flags import effective

Consumer = Literal["dashboard", "notification", "export", "doctor_pdf", "ai_context"]
CONSUMERS: Final = get_args(Consumer)
# Section 11: the export carries every finding with its overlay state; the others leave a hidden
# finding out.
INCLUDES_HIDDEN: Final = frozenset({"export"})
Tab = Literal["live", "history"]
TABS: Final = get_args(Tab)
# The tab is the origin (PLAN §7.1 step 7): live findings, and those found while backfilling.
_ORIGIN: Final = {"live": "live", "history": "backfill"}
FINDING_TIERS: Final = ("device", "trend", "experimental", "system")
EXPERIMENTAL: Final = "experimental"
# The design's area filter (Findings mock-up) by the rule's letter; Q1 is the system area.
AREAS: Final = ("body", "heart", "nerve", "activity", "system")
_AREA_BY_LETTER: Final = {
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
READINGS_LIMIT: Final = 500

_COLUMNS = (
    "id, rule_id, rule_version, tier, severity, confidence, data_quality, origin, metric_keys, "
    "subject, window_start, window_end, reference_day, evidence, input_revision, status, "
    "first_seen, last_seen, ended_at, notify_state, notified_at, seen_at"
)
_FINDINGS = text(
    f"SELECT {_COLUMNS} FROM findings "  # noqa: S608 - constant column list
    "WHERE owner_id = :owner "
    "AND (CAST(:origin AS text) IS NULL OR origin = CAST(:origin AS text)) "
    "AND (CAST(:tier AS text) IS NULL OR tier = CAST(:tier AS text)) "
    "AND (CAST(:finding AS uuid) IS NULL OR id = CAST(:finding AS uuid)) "
    "AND (CAST(:experimental AS boolean) OR tier <> 'experimental') "
    "ORDER BY last_seen DESC, id"
)
_GROUPS = text(
    "SELECT g.grpid, g.connection_id, g.measured_at, g.hash_deviceid, g.model_id, g.raw, "
    "ARRAY(SELECT m.type FROM measures AS m "
    "WHERE m.owner_id = g.owner_id AND m.measure_group_id = g.id) AS types "
    "FROM measure_groups AS g "
    "WHERE g.owner_id = :owner AND g.deleted_at IS NULL "
    "AND g.grpid = ANY(CAST(:groups AS bigint[]))"
)
_RECORDING_COLUMNS = (
    "r.id, r.connection_id, r.recorded_at, r.model_id, r.reported, r.signalid, "
    "r.payload_hash, r.sampling_hz, r.estimated"
)
_LINKABLE_RECORDINGS = text(
    f"SELECT {_RECORDING_COLUMNS} FROM ecg_recordings AS r "  # noqa: S608 - constant columns
    "WHERE r.owner_id = :owner AND EXISTS ("
    "SELECT 1 FROM measure_groups AS g WHERE g.owner_id = r.owner_id "
    "AND g.connection_id = r.connection_id AND g.measured_at = r.recorded_at "
    "AND g.deleted_at IS NULL AND g.grpid = ANY(CAST(:groups AS bigint[])))"
)
_RECORDINGS_BY_ID = text(
    f"SELECT {_RECORDING_COLUMNS} FROM ecg_recordings AS r "  # noqa: S608 - constant columns
    "WHERE r.owner_id = :owner AND r.id = ANY(CAST(:ids AS uuid[]))"
)
_READINGS = text(
    "SELECT metric_key, ts, value, source_grpid, quality_flags FROM observations "
    "WHERE owner_id = :owner AND metric_key = ANY(CAST(:keys AS text[])) "
    "AND source_grpid = ANY(CAST(:groups AS bigint[])) "
    "ORDER BY ts, metric_key LIMIT :limit"
)

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class FindingsQuery:
    """The list filters: ``tab`` (``live``/``history``), ``tier`` and ``area``; ``None`` is all."""

    tab: Tab | None = None
    tier: str | None = None
    area: str | None = None

    def __post_init__(self) -> None:
        if self.tab is not None and self.tab not in TABS:
            raise ValueError("unknown tab")
        if self.tier is not None and self.tier not in FINDING_TIERS:
            raise ValueError("unknown tier")
        if self.area is not None and self.area not in AREAS:
            raise ValueError("unknown area")


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
        return _trigger_groups(self.evidence)


@dataclass(frozen=True)
class FindingsRead:
    findings: tuple[FindingView, ...]
    experimental: bool  # whether the switch was on for this read

    def __repr__(self) -> str:
        return f"FindingsRead({len(self.findings)} findings)"


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


def area_of(rule_id: str) -> str | None:
    return _AREA_BY_LETTER.get(rule_id[:1]) if rule_id else None


def _is_experimental(tier: str, rule_id: str) -> bool:
    return tier == EXPERIMENTAL or rule_id in EXPERIMENTAL_RULES


def _trigger_groups(evidence: Mapping[str, Any]) -> tuple[int, ...]:
    raw = evidence.get("trigger_groups")
    if not isinstance(raw, list):
        return ()
    found: list[int] = []
    for value in raw:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(value, int) and not isinstance(value, bool):
            found.append(value)
    return tuple(dict.fromkeys(found))


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _group(row: Mapping[Any, Any]) -> GroupKey:
    types = row["types"] or ()
    return GroupKey(
        grpid=int(row["grpid"]),
        connection=row["connection_id"],
        measured_at=row["measured_at"],
        device_ids=group_device_ids(row["hash_deviceid"], row["raw"]),
        model_id=_int(row["model_id"]),
        types=frozenset(int(t) for t in types),
    )


def _recording(row: Mapping[Any, Any]) -> RecordingKey:
    digest = row["payload_hash"]
    estimated = row["estimated"]
    return RecordingKey(
        id=row["id"],
        connection=row["connection_id"],
        recorded_at=row["recorded_at"],
        device_id=recording_device_id(row["reported"]),
        model_id=_int(row["model_id"]),
        fingerprint=input_fingerprint(
            int(row["signalid"]),
            None if digest is None else bytes(digest),
            _int(row["sampling_hz"]),
        ),
        estimated=estimated if isinstance(estimated, Mapping) else None,  # pyright: ignore[reportUnknownArgumentType]
    )


async def _switch(db: AsyncSession, settings: Settings) -> bool:
    return await effective.experimental_findings(db, settings)


async def _links(
    db: AsyncSession, owner: UUID, groups: Sequence[int]
) -> tuple[tuple[GroupKey, ...], tuple[RecordingKey, ...]]:
    if not groups:
        return (), ()
    params = {"owner": owner, "groups": list(groups)}
    group_rows = (await db.execute(_GROUPS, params)).mappings().all()
    recording_rows = (await db.execute(_LINKABLE_RECORDINGS, params)).mappings().all()
    return (
        tuple(_group(row) for row in group_rows),
        tuple(_recording(row) for row in recording_rows),
    )


def _view(row: Mapping[Any, Any], overlay: Overlay, switch: bool) -> FindingView:
    rule_id = str(row["rule_id"])
    gated = overlay.applies and rule_id in GATED_RULES
    return FindingView(
        id=row["id"],
        rule_id=rule_id,
        rule_version=str(row["rule_version"]),
        tier=str(row["tier"]),
        area=area_of(rule_id),
        severity=str(row["severity"]),
        confidence=str(row["confidence"]),
        data_quality=_mapping(row["data_quality"]),
        origin=str(row["origin"]),
        metric_keys=tuple(row["metric_keys"] or ()),
        subject=str(row["subject"]),
        window_start=row["window_start"],
        window_end=row["window_end"],
        reference_day=row["reference_day"],
        evidence=_mapping(row["evidence"]),
        status=str(row["status"]),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        ended_at=row["ended_at"],
        notify_state=str(row["notify_state"]),
        notified_at=row["notified_at"],
        seen_at=row["seen_at"],
        hidden=overlay.hidden,
        not_evaluated=gated and overlay.outcome == "not_evaluated",
        gate=(
            GateView(overlay.outcome, overlay.counted, overlay.failed)
            if switch and overlay.applies
            else None
        ),
    )


async def _load(
    db: AsyncSession,
    owner: UUID,
    switch: bool,
    consumer: Consumer,
    query: FindingsQuery,
    finding: UUID | None,
) -> tuple[list[FindingView], dict[UUID, dict[int, tuple[RecordingKey, ...]]]]:
    if consumer not in CONSUMERS:
        raise ValueError("unknown consumer")
    params = {
        "owner": owner,
        "origin": None if query.tab is None else _ORIGIN[query.tab],
        "tier": query.tier,
        "finding": finding,
        "experimental": switch,
    }
    rows = (await db.execute(_FINDINGS, params)).mappings().all()
    kept = [
        row
        for row in rows
        if (switch or not _is_experimental(str(row["tier"]), str(row["rule_id"])))
        and (query.area is None or area_of(str(row["rule_id"])) == query.area)
    ]
    groups = sorted({g for row in kept for g in _trigger_groups(_mapping(row["evidence"]))})
    group_keys, recordings = await _links(db, owner, groups)
    views: list[FindingView] = []
    linked_by_finding: dict[UUID, dict[int, tuple[RecordingKey, ...]]] = {}
    for row in kept:
        triggers = _trigger_groups(_mapping(row["evidence"]))
        linked = resolve_links(triggers, group_keys, recordings)
        overlay = ecg_gate_overlay(str(row["rule_id"]), linked)
        if overlay.hidden and consumer not in INCLUDES_HIDDEN:
            continue
        views.append(_view(row, overlay, switch))
        linked_by_finding[row["id"]] = linked
    return views, linked_by_finding


async def read_findings(
    factory: SessionFactory,
    ctx: UserContext,
    settings: Settings,
    *,
    consumer: Consumer,
    query: FindingsQuery | None = None,
) -> FindingsRead:
    """The signed-in user's findings for ``consumer``, filtered by ``query``."""
    query = query if query is not None else FindingsQuery()
    async with user_session(factory, ctx) as db:
        switch = await _switch(db, settings)
        views, _ = await _load(db, ctx.user_id, switch, consumer, query, None)
    return FindingsRead(findings=tuple(views), experimental=switch)


def _readings(rows: Iterable[Mapping[Any, Any]]) -> tuple[Reading, ...]:
    return tuple(
        Reading(
            metric_key=str(row["metric_key"]),
            ts=row["ts"],
            value=Decimal(row["value"]),
            grpid=_int(row["source_grpid"]),
            quality_flags=tuple(row["quality_flags"] or ()),
        )
        for row in rows
    )


async def read_finding(
    factory: SessionFactory,
    ctx: UserContext,
    settings: Settings,
    finding_id: UUID,
    *,
    consumer: Consumer = "dashboard",
) -> FindingDetail | None:
    """One finding with the readings that drove it and, behind the switch, the up-to-date
    estimates of its linked recordings. ``None`` when the user may not see it here: another
    user's, missing, experimental without the switch, or hidden for this consumer."""
    async with user_session(factory, ctx) as db:
        switch = await _switch(db, settings)
        views, linked = await _load(db, ctx.user_id, switch, consumer, FindingsQuery(), finding_id)
        if not views:
            return None
        view = views[0]
        groups = view.trigger_groups
        rows = (
            (
                await db.execute(
                    _READINGS,
                    {
                        "owner": ctx.user_id,
                        "keys": list(view.metric_keys),
                        "groups": list(groups),
                        "limit": READINGS_LIMIT,
                    },
                )
            )
            .mappings()
            .all()
            if groups and view.metric_keys
            else []
        )
    estimates: list[LinkedEstimate] = []
    if switch:
        seen: set[UUID] = set()
        for grpid, recordings in linked.get(view.id, {}).items():
            for recording in recordings:
                if recording.id in seen:
                    continue
                seen.add(recording.id)
                shown = estimate_view(recording.estimated, recording.fingerprint)
                estimates.append(LinkedEstimate(recording.id, grpid, shown))
    return FindingDetail(finding=view, readings=_readings(rows), estimates=tuple(estimates))


async def read_ecg_estimates(
    factory: SessionFactory,
    ctx: UserContext,
    settings: Settings,
    recording_ids: Sequence[UUID],
) -> dict[UUID, EstimateView]:
    """Section 9's up-to-date estimates of the user's recordings, for the ECG viewer and the
    other outputs. Empty without the switch: our estimates are experimental (section 9)."""
    if not recording_ids:
        return {}
    async with user_session(factory, ctx) as db:
        if not await _switch(db, settings):
            return {}
        rows = (
            (
                await db.execute(
                    _RECORDINGS_BY_ID, {"owner": ctx.user_id, "ids": list(recording_ids)}
                )
            )
            .mappings()
            .all()
        )
    found = (_recording(row) for row in rows)
    return {r.id: estimate_view(r.estimated, r.fingerprint) for r in found}


__all__ = [
    "AREAS",
    "CONSUMERS",
    "EXPERIMENTAL_RULES",
    "TABS",
    "Consumer",
    "FindingDetail",
    "FindingView",
    "FindingsQuery",
    "FindingsRead",
    "GateView",
    "LinkedEstimate",
    "Reading",
    "area_of",
    "read_ecg_estimates",
    "read_finding",
    "read_findings",
]
