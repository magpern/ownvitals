"""The finding notifications (PLAN §7.1 step 7, ADR 0010 §4, ADR 0006 section 11; #195): the
``notify_findings`` job kind and its pass.

**What notifies.** Only ``live`` episodes, once, at their first write: the rules engine inserts
them with ``notify_state`` ``pending`` (a backfill episode is ``none`` and the schema keeps it
so; an update keeps the state it has). This pass reads the owner's ``pending`` and ``held``
findings and moves each exactly once to ``sent`` or ``none``; nothing else ever moves a finding
back, so a sent notification is never recalled and an update never notifies.

**How it reads.** The ids of the findings still waiting, and every move of their state, are
``notify.findings_state`` (the one module of the step that names the table); everything the
decision shows or hides comes from the one read function
(``views.findings_read.read_notification_candidates``, the ``notification`` consumer): the
experimental tier only with the owner-only switch (D9), the overlay, and the trigger groups'
links. A candidate the read function does not return (an experimental one without the switch)
is never sent.

**The hold** (``notify.hold``, pure). E4 and E6 wait for the gate: the first pass that sees one
starts its ``notify_holds`` row (``held``); each pass decides again from the current links, the
``ecg`` cursors and the completed ``ecg`` sync jobs; a new revision restarts the hold; the bound
is ``NotifySettings.hold_bound`` from the first. Every end writes one reason code. A hold whose
finding is gone (or no longer waiting to notify) ends ``withdrawn``. E1 to E3 and the other
rules are sent at once.

**Delivery.** A finding of a tier the user notifies (``user_profiles.notify_tiers``, every tier
by default) becomes ``sent`` (the in-app list, ``views.notifications_routes``) and, when the
user asked for email (``notify_email``), queues one ``findings_waiting`` outbox row (coalesced
per owner; the mail says only that something is waiting). Any other tier becomes ``none``.

**Exactly once.** The pass runs as the owner in one fenced transaction. Every move is a
conditional ``UPDATE ... WHERE notify_state IN (...) RETURNING``, row-locked, and the outbox row
is queued only after the move returned the row, in the same transaction: of two workers on the
same finding, the second finds it moved and does nothing. Findings are taken in id order, and a
finding's row is always locked before its hold's.

The job kind and when it is queued are ``notify.findings_job``.
"""

import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.config import Settings
from ownvitals.derived.tables import DEFAULT_NOTIFY_TIERS
from ownvitals.notify import findings_state as state
from ownvitals.notify.hold import (
    EcgFacts,
    EcgStream,
    EcgSync,
    Hold,
    decide,
    held,
    new_revision,
)
from ownvitals.notify.tables import HoldOutcome, Reason
from ownvitals.views.findings_read import NotificationCandidate, read_notification_candidates

logger = logging.getLogger(__name__)

SENT: Final = ("sent", "sent_not_evaluated")
# The jobs that sync the ``ecg`` stream (``withings.sync_kinds``).
ECG_SYNC_KINDS: Final = ("withings_sync", "withings_backfill")

OpenTx = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_HOLDS = text(
    "SELECT finding_id, started_at, revision_at, revision_seen, revision_groups "
    "FROM notify_holds WHERE owner_id = :o AND ended_at IS NULL"
)
_PROFILE = text("SELECT notify_email, notify_tiers FROM user_profiles WHERE owner_id = :o")
_CURSORS = text(
    "SELECT connection_id, capability, backfill_state->>'status' AS status, last_ok_at "
    "FROM sync_cursors WHERE owner_id = :o AND stream = 'ecg'"
)
_SYNCS = text(
    "SELECT input->>'connection_id' AS connection, started_at FROM jobs "
    "WHERE owner_id = :o AND state = 'completed' AND started_at >= :since "
    "AND (kind = 'ecg_fetch' OR (kind = ANY(CAST(:kinds AS text[])) "
    "AND input->>'stream' = 'ecg'))"
)
_START = text(
    "INSERT INTO notify_holds (owner_id, finding_id, rule_id, started_at, revision_at, "
    "revision_seen, revision_groups) VALUES (:o, :id, :rule, :now, :now, :seen, "
    "CAST(:groups AS bigint[])) ON CONFLICT (owner_id, finding_id) DO NOTHING"
)
_RESTART = text(
    "UPDATE notify_holds SET revision_at = :now, revision_seen = :seen, "
    "revision_groups = CAST(:groups AS bigint[]) "
    "WHERE owner_id = :o AND finding_id = :id AND ended_at IS NULL"
)
_END = text(
    "UPDATE notify_holds SET ended_at = :now, reason = :reason, outcome = :outcome "
    "WHERE owner_id = :o AND finding_id = :id AND ended_at IS NULL"
)


@dataclass(frozen=True)
class Preferences:
    email: bool = False
    tiers: frozenset[str] = frozenset(DEFAULT_NOTIFY_TIERS)


@dataclass
class NotifyOutcome:
    """Counts only."""

    sent: int = 0
    dropped: int = 0
    held: int = 0
    ended: int = 0
    withdrawn: int = 0


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


async def _preferences(db: AsyncSession, owner: UUID) -> Preferences:
    row = (await db.execute(_PROFILE, {"o": owner})).first()
    if row is None:
        return Preferences()
    return Preferences(email=bool(row.notify_email), tiers=frozenset(row.notify_tiers or ()))


async def _facts(db: AsyncSession, owner: UUID, since: datetime | None) -> EcgFacts:
    """The ``ecg`` cursors, and the completed ``ecg`` syncs that started at or after ``since``
    (the oldest open hold's revision start; none when no hold was open: a hold started or
    restarted in this pass counts only syncs that start after it)."""
    streams = {
        row.connection_id: EcgStream(row.capability, row.status == "complete", row.last_ok_at)
        for row in (await db.execute(_CURSORS, {"o": owner})).all()
    }
    syncs: list[EcgSync] = []
    params = {"o": owner, "kinds": list(ECG_SYNC_KINDS), "since": since}
    for row in (await db.execute(_SYNCS, params)).all() if since is not None else ():
        connection = _uuid(row.connection)
        if connection is not None:
            syncs.append(EcgSync(connection, row.started_at))
    return EcgFacts(streams, tuple(syncs))


async def _holds(db: AsyncSession, owner: UUID) -> dict[UUID, Hold]:
    return {
        row.finding_id: Hold(
            row.started_at,
            row.revision_at,
            row.revision_seen,
            tuple(int(g) for g in row.revision_groups or ()),
        )
        for row in (await db.execute(_HOLDS, {"o": owner})).all()
    }


async def _end(
    db: AsyncSession,
    owner: UUID,
    finding: UUID,
    reason: Reason,
    outcome: HoldOutcome,
    now: datetime,
) -> None:
    params = {"o": owner, "id": finding, "now": now, "reason": reason, "outcome": outcome}
    await db.execute(_END, params)


async def _finish(
    db: AsyncSession,
    owner: UUID,
    candidate: NotificationCandidate,
    decided: HoldOutcome,
    reason: Reason | None,
    prefs: Preferences,
    now: datetime,
    counts: NotifyOutcome,
) -> None:
    view = candidate.finding
    deliver = decided in SENT and view.tier in prefs.tiers
    if deliver:
        moved = await state.send(db, owner, view.id, now, email=prefs.email)
    else:
        moved = await state.drop(db, owner, view.id)
    if reason is not None:  # a held finding: its hold ends with this pass
        if not moved:  # gone meanwhile, or another worker moved it first
            await _end(db, owner, view.id, "withdrawn", "none", now)
        else:
            outcome: HoldOutcome = decided if deliver or decided == "hidden" else "none"
            await _end(db, owner, view.id, reason, outcome, now)
        counts.ended += 1
    if not moved:
        return
    if not deliver:
        counts.dropped += 1
        return
    counts.sent += 1


async def _pass(
    db: AsyncSession, owner: UUID, settings: Settings, bound: timedelta, now: datetime
) -> NotifyOutcome:
    counts = NotifyOutcome()
    ids = await state.waiting_ids(db, owner)
    candidates = await read_notification_candidates(db, owner, settings, ids)
    shown = {c.finding.id for c in candidates}
    for missing in (i for i in ids if i not in shown):  # not visible to this account: never sent
        if await state.drop(db, owner, missing):
            counts.dropped += 1
    prefs = await _preferences(db, owner)
    holds = await _holds(db, owner)
    since = min((h.revision_at for h in holds.values()), default=None)
    facts = await _facts(db, owner, since)
    for candidate in sorted(candidates, key=lambda c: c.finding.id):  # one lock order
        view = candidate.finding
        if view.origin != "live" or view.notify_state not in ("pending", "held"):
            continue
        if not held(view.rule_id):
            decided = "hidden" if view.hidden else "sent"
            await _finish(db, owner, candidate, decided, None, prefs, now, counts)
            continue
        groups = sorted(set(view.trigger_groups))
        params: Mapping[str, Any] = {
            "o": owner,
            "id": view.id,
            "now": now,
            "seen": view.last_seen,
            "groups": groups,
        }
        hold = holds.get(view.id)
        if hold is None:
            await state.mark_held(db, owner, view.id)  # the finding's row first, then its hold's
            await db.execute(_START, {**params, "rule": view.rule_id})
            hold = Hold(now, now, view.last_seen, tuple(groups))
        elif new_revision(hold, view.last_seen, groups):
            await db.execute(_RESTART, params)
            hold = Hold(hold.started_at, now, view.last_seen, tuple(groups))
        decision = decide(
            rule_id=view.rule_id,
            hidden=view.hidden,
            not_evaluated=view.not_evaluated,
            links=candidate.links,
            facts=facts,
            hold=hold,
            now=now,
            bound=bound,
        )
        if decision.wait or decision.outcome is None or decision.reason is None:
            counts.held += 1
            continue
        await _finish(db, owner, candidate, decision.outcome, decision.reason, prefs, now, counts)
    counts.withdrawn = await state.withdraw_orphans(db, owner, now)
    return counts


async def notify_owner(
    open_tx: OpenTx,
    owner: UUID,
    *,
    settings: Settings,
    bound: timedelta,
    now: datetime | None = None,
) -> NotifyOutcome:
    """One owner's pass, in one transaction opened by ``open_tx`` (a job passes its fenced
    transaction, a test a user session)."""
    moment = now or datetime.now(UTC)
    async with open_tx() as db:
        return await _pass(db, owner, settings, bound, moment)


__all__ = ["NotifyOutcome", "Preferences", "notify_owner"]
