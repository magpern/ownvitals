"""The wait for the admin delete's Withings revoke (#365, PLAN section 4.3, ADR 0002 section 3).

``DELETE /api/admin/users/{user_id}`` (``ownvitals.admin.users``) first queues the target's
``withings_admin_revoke`` job in its own audited transaction (``user.revoke``,
``ownvitals.auth.admin_accounts.revoke_account``), which commits so the worker can claim it.
``wait_for_revoke`` then reads only that job's ``state``, through ``ownvitals_maint``'s column
grant on ``jobs`` (no owner, no input), in one audited read transaction (``user.revoke_wait``),
for up to ``ADMIN_REVOKE_WAIT_SECONDS``: ``revoked`` when it completed, ``failed`` otherwise
(failed, dead-lettered, cancelled, gone, or still queued or running at the deadline). The delete
then goes ahead either way and records the outcome; a job still waiting goes with the account.
"""

import asyncio
import time
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.admin.audit import Target
from ownvitals.admin.db import Actor, AdminDb, admin_session

# How long the admin delete waits for the worker's revoke (the user's own delete waits
# ``connections.routes.REVOKE_WAIT_SECONDS``, the same 25 s).
ADMIN_REVOKE_WAIT_SECONDS: float = 25.0
_POLL_SECONDS: float = 0.25
_STATE: Final = text("SELECT state FROM jobs WHERE id = :id")
_WAITING: Final = frozenset({"queued", "running"})
Revoke = Literal["none", "revoked", "failed"]


async def _ended(db: AsyncSession, job_id: UUID) -> str | None:
    """The job's state once it left ``queued`` and ``running`` (``None`` when it is gone), or
    ``"running"`` at the deadline."""
    deadline = time.monotonic() + ADMIN_REVOKE_WAIT_SECONDS
    while True:
        state: str | None = (await db.execute(_STATE, {"id": job_id})).scalar_one_or_none()
        if state not in _WAITING or time.monotonic() >= deadline:
            return state
        await asyncio.sleep(_POLL_SECONDS)


async def wait_for_revoke(db: AdminDb, actor: Actor, user_id: UUID, job_id: UUID | None) -> Revoke:
    """How the revoke ``job_id`` (queued for ``user_id``) ended: ``none`` without a job."""
    if job_id is None:
        return "none"
    target = Target(user_id=user_id)
    async with admin_session(db, actor, "user.revoke_wait", target=target) as session:
        state = await _ended(session, job_id)
    return "revoked" if state == "completed" else "failed"
