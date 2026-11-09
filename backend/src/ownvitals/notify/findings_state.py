"""The notification state of a finding (ADR 0010 §4, ADR 0006 section 11; #195): the one module
of the notification step that names the ``findings`` table.

It reads the ids of the owner's live findings still waiting to notify, and moves
``notify_state`` (``pending`` to ``held``; ``pending`` or ``held`` to ``sent`` or ``none``) with
conditional, row-locking updates that return whether they moved the row. It never reads what a
finding says: what the step shows or hides comes only through the read function
(``views.findings_read``; ``tests/test_findings_read_guard.py`` lists this module by name). Every
statement filters ``owner_id`` itself and runs in the owner's session under forced RLS.

``send`` queues the ``findings_waiting`` outbox row in the same transaction, only when it moved
the row, so a finding mails at most once whoever runs the step.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.notify.outbox import queue_user_mail

_WAITING = text(
    "SELECT id FROM findings WHERE owner_id = :o AND origin = 'live' "
    "AND notify_state IN ('pending', 'held') ORDER BY id"
)
_HELD = text(
    "UPDATE findings SET notify_state = 'held' "
    "WHERE owner_id = :o AND id = :id AND notify_state = 'pending'"
)
_SEND = text(
    "UPDATE findings SET notify_state = 'sent', notified_at = :now "
    "WHERE owner_id = :o AND id = :id AND notify_state IN ('pending', 'held') RETURNING id"
)
_DROP = text(
    "UPDATE findings SET notify_state = 'none' "
    "WHERE owner_id = :o AND id = :id AND notify_state IN ('pending', 'held') RETURNING id"
)
# A hold whose finding is gone, or no longer waiting to notify, ends ``withdrawn``.
_WITHDRAWN = text(
    "UPDATE notify_holds AS h SET ended_at = :now, reason = 'withdrawn', outcome = 'none' "
    "WHERE h.owner_id = :o AND h.ended_at IS NULL AND NOT EXISTS (SELECT 1 FROM findings AS f "
    "WHERE f.owner_id = h.owner_id AND f.id = h.finding_id "
    "AND f.notify_state IN ('pending', 'held'))"
)


async def waiting_ids(db: AsyncSession, owner: UUID) -> list[UUID]:
    """The owner's live findings whose notification is ``pending`` or ``held``, by id."""
    return [row.id for row in (await db.execute(_WAITING, {"o": owner})).all()]


async def mark_held(db: AsyncSession, owner: UUID, finding: UUID) -> None:
    await db.execute(_HELD, {"o": owner, "id": finding})


async def send(db: AsyncSession, owner: UUID, finding: UUID, now: datetime, *, email: bool) -> bool:
    """Move the finding to ``sent`` and, when ``email``, queue the outbox row (coalesced per
    owner). False when it was not waiting any more (moved by another pass, or gone)."""
    moved = (await db.execute(_SEND, {"o": owner, "id": finding, "now": now})).first()
    if moved is None:
        return False
    if email:
        await queue_user_mail(db, owner, "findings_waiting", "findings")
    return True


async def drop(db: AsyncSession, owner: UUID, finding: UUID) -> bool:
    """Move the finding to ``none``: it never notifies. False when it was not waiting."""
    return (await db.execute(_DROP, {"o": owner, "id": finding})).first() is not None


async def withdraw_orphans(db: AsyncSession, owner: UUID, now: datetime) -> int:
    """End every open hold whose finding is gone or no longer waiting: ``withdrawn``."""
    result = await db.execute(_WITHDRAWN, {"o": owner, "now": now})
    return int(getattr(result, "rowcount", 0) or 0)
