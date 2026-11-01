"""Linked sign-in methods on the account page (ADR 0003 sections 1, 5 and 6).

Listing needs a session. Unlinking needs recent authentication (and step-up with a confirmed
TOTP), only touches the signed-in user's own identities, answers 404 for another user's id as for
an unknown one, and refuses to remove the last sign-in method.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from ownvitals.auth.deps import request_ip, require_recent_auth, require_user
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory
from ownvitals.auth.oauth_store import list_identities, remove_identity
from ownvitals.auth.tables import users
from ownvitals.context import UserContext
from ownvitals.db import system_session

router = APIRouter(prefix="/api/auth", tags=["auth"])


class IdentityOut(BaseModel):
    id: UUID
    provider: str
    email_at_link: str | None
    created_at: datetime


class IdentitiesOut(BaseModel):
    identities: list[IdentityOut]
    has_password: bool
    available: list[str]


@router.get("/identities")
async def get_identities(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)]
) -> IdentitiesOut:
    async with system_session(factory(request)) as db:
        rows = await list_identities(db, ctx.user_id)
        has_password = (
            await db.execute(
                select(users.c.password_hash.is_not(None)).where(users.c.id == ctx.user_id)
            )
        ).scalar_one()
    return IdentitiesOut(
        identities=[IdentityOut.model_validate(row, from_attributes=True) for row in rows],
        has_password=has_password,
        available=sorted(request.app.state.oauth.providers),
    )


@router.delete("/identities/{identity_id}", status_code=204)
async def unlink_identity(
    identity_id: str, request: Request, ctx: Annotated[UserContext, Depends(require_recent_auth)]
) -> None:
    try:
        target = UUID(identity_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    async with system_session(factory(request)) as db:
        result = await remove_identity(db, ctx.user_id, target)
        if result == "removed":
            await record_event(db, "identity_unlinked", user_id=ctx.user_id, ip=request_ip(request))
    if result == "gone":
        raise HTTPException(status_code=404)  # unknown, or another user's: the same answer
    if result == "last":
        raise HTTPException(status_code=409, detail={"reason": "last_sign_in_method"})
