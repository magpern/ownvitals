"""``/api/admin``: M1 ships only ``whoami``, which proves the MFA gate (ADR 0003 section 7).
The admin area itself is M5."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ownvitals.auth.deps import current_session, require_admin
from ownvitals.auth.sessions import SessionInfo
from ownvitals.context import UserContext

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminWhoami(BaseModel):
    id: UUID
    email: str
    role: str


@router.get("/whoami")
async def whoami(
    ctx: Annotated[UserContext, Depends(require_admin)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> AdminWhoami:
    """401 anonymous, 403 ``forbidden`` for a non-admin, 403 ``mfa_required`` for an admin
    whose session has not passed TOTP in the last 12 hours."""
    return AdminWhoami(id=ctx.user_id, email=info.email if info else "", role=ctx.role)
