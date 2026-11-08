"""``auth_events``: the security event log (ADR 0003 sections 7 and 10).

A row holds the kind, the user, the address and a small ``detail``. Never a password, token or
code. Rows are purged after 30 days.

A row written during a web request also holds the request's coarse origin (``event_origin``:
browser family, operating system, place), which the user's own activity list shows (#251)
instead of the address.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.event_origin import current_origin
from ownvitals.auth.tables import auth_events


def event_values(
    kind: str,
    *,
    user_id: UUID | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The column values of one row, with the current request's origin when there is one."""
    origin = current_origin()
    return {
        "kind": kind,
        "user_id": user_id,
        "ip": ip,
        "detail": detail,
        "browser": origin.browser if origin else None,
        "os": origin.os if origin else None,
        "place": origin.place if origin else None,
    }


async def record_event(
    db: AsyncSession,
    kind: str,
    *,
    user_id: UUID | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        insert(auth_events).values(**event_values(kind, user_id=user_id, ip=ip, detail=detail))
    )
