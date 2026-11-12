"""Password change (ADR 0003 sections 5 and 6): recent authentication and a step-up, revoking
every other session. Moved out of ``reset_routes`` unchanged."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import update

from ownvitals.auth.deps import current_session, request_ip, require_sensitive
from ownvitals.auth.events import record_event
from ownvitals.auth.flowtools import factory, reserve
from ownvitals.auth.passwords import PasswordRuleError, normalize_password
from ownvitals.auth.ratelimit import PASSWORD_CHANGE, Bucket
from ownvitals.auth.sessions import SessionInfo, revoke_user_sessions
from ownvitals.auth.signup_routes import refuse
from ownvitals.auth.tables import users
from ownvitals.auth.tokens import delete_outstanding_tokens
from ownvitals.auth.userlock import lock_user_row
from ownvitals.context import UserContext
from ownvitals.db import system_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

FIELD_MAX = 1024


class ChangePasswordIn(BaseModel):
    new_password: str = Field(max_length=FIELD_MAX)


class Done(BaseModel):
    status: str = "ok"


@router.post("/password/change")
async def change_password(
    body: ChangePasswordIn,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_sensitive)],
    info: Annotated[SessionInfo | None, Depends(current_session)],
) -> Done:
    """Set or change the password. Every other session is revoked; the caller's stays."""
    if info is None:
        raise HTTPException(status_code=401, detail={"reason": "not_authenticated"})
    try:
        password = normalize_password(body.new_password)
    except PasswordRuleError:
        raise refuse(400, "password_rule") from None
    await reserve(request, [Bucket(PASSWORD_CHANGE, str(ctx.user_id))])
    password_hash = await request.app.state.password_hasher.hash(password)
    async with system_session(factory(request)) as db:
        await lock_user_row(db, ctx.user_id)  # first (``userlock``)
        await db.execute(
            update(users).where(users.c.id == ctx.user_id).values(password_hash=password_hash)
        )
        await revoke_user_sessions(db, ctx.user_id, except_id=info.id)
        await delete_outstanding_tokens(db, ctx.user_id)
        await record_event(db, "password_changed", user_id=ctx.user_id, ip=request_ip(request))
    return Done()
