"""The admin page's account writes (issue 550, ADR 0003 sections 4 and 7), on the router shell.

Every route has ``admin_gate`` (for these writes: a step-up within 5 minutes, refused 403
``step_up_required`` with an ``admin.request`` row and nothing changed) and also depends on
``require_admin_stepup``, as the Users view's writes do; the gate runs first, so a missing
step-up is still the audited refusal. Each change runs inside its ``admin_session`` (#314): the
``ok`` audit row is that transaction's first statement, then the definer of
``70-admin-accounts.sql`` behind ``ownvitals.auth.admin_writes``, which refuses without that row.
A refusal rolls the ``ok`` row back and writes one ``refused`` row.

- ``POST /invites`` (``invite.create``, params ``expires_days``): one invite as the operator CLI's
  ``invite create`` stores it (the code's SHA-256 and the expiry; ``INVITE_DAYS``, the CLI's
  default). 201 with the code, shown this once, and its expiry. The code is in no audit row, log
  or other response; the gate's ``no-store`` keeps it out of caches.
- ``DELETE /users/{user_id}/sessions`` (``sessions.revoke_all``, the account as the target):
  signs the account out everywhere, an admin's (the caller's own included) as well. 404
  ``not_found`` for an unknown account. Answers how many sessions went.

No route here changes a role or resets a second factor: those stay with the operator CLI.
"""

from datetime import datetime, timedelta
from typing import Annotated, Final
from uuid import UUID

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict

from ownvitals.admin.audit import Target
from ownvitals.admin.db import AdminRefused, admin_session
from ownvitals.admin.routes import AdminActor, admin_router, require_db
from ownvitals.auth.admin_accounts import AccountMissing
from ownvitals.auth.admin_writes import create_admin_invite, sign_out_account
from ownvitals.auth.deps import require_admin_stepup
from ownvitals.context import UserContext

router = admin_router()

INVITE_DAYS: Final = 7  # ``ownvitals invite create``'s default


def _same_admin(ctx: UserContext, actor: AdminActor) -> None:
    """The step-up dependency and the gate resolved the same admin."""
    if ctx.user_id != actor.user_id:
        raise AdminRefused("forbidden", 403)


class Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class InviteOut(Out):
    code: str
    expires_at: datetime


class SignedOutOut(Out):
    id: UUID
    sessions_revoked: int


@router.post("/invites", response_model=InviteOut, status_code=201)
async def create_invite(
    request: Request,
    actor: AdminActor,
    ctx: Annotated[UserContext, Depends(require_admin_stepup)],
) -> InviteOut:
    _same_admin(ctx, actor)
    async with admin_session(
        require_db(request),
        actor,
        "invite.create",
        write=True,
        target=Target(kind="invite"),
        params={"expires_days": INVITE_DAYS},
    ) as db:
        invite = await create_admin_invite(db, timedelta(days=INVITE_DAYS))
    # Only once the row and its audit row committed is the code shown.
    return InviteOut(code=invite.code, expires_at=invite.expires_at)


@router.delete("/users/{user_id}/sessions", response_model=SignedOutOut)
async def sign_out_all(
    request: Request,
    actor: AdminActor,
    user_id: UUID,
    ctx: Annotated[UserContext, Depends(require_admin_stepup)],
) -> SignedOutOut:
    _same_admin(ctx, actor)
    async with admin_session(
        require_db(request),
        actor,
        "sessions.revoke_all",
        write=True,
        target=Target(user_id=user_id),
    ) as db:
        try:
            revoked = await sign_out_account(db, user_id)
        except AccountMissing:
            raise AdminRefused("not_found", 404) from None
    return SignedOutOut(id=user_id, sessions_revoked=revoked)
