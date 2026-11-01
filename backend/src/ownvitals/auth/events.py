"""``auth_events``: the security event log (ADR 0003 sections 7 and 10).

A row holds the kind, the user, the address and a small ``detail``. Never a password, token or
code. Rows are purged after 30 days.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.tables import auth_events


async def record_event(
    db: AsyncSession,
    kind: str,
    *,
    user_id: UUID | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await db.execute(insert(auth_events).values(kind=kind, user_id=user_id, ip=ip, detail=detail))
