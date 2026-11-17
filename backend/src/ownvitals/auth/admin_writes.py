"""The admin page's account writes (issue 550, guard 8): "Sign out all" for one account and an
admin's invite.

Both take the session of ``ownvitals.admin.db.admin_session``, whose ``ok`` audit row is that
transaction's first statement, and call their definer of
``docker/postgres/maint/70-admin-accounts.sql`` (ADR 0002 section 3), which refuses unless that
row is there: the change and its row commit or roll back together (#314). ``ownvitals_maint``
holds no grant on ``sessions`` or ``invites``; the admin area calls only these names.

- ``sign_out_account``: every session of the account deleted (``sessions.revoke_all``). Unlike
  disable it takes an admin too, the acting admin included: it leaves no lasting state.
- ``create_admin_invite``: one invite as the operator CLI stores it (``invite.create``): the
  code's SHA-256 and the expiry, no ``created_by``. The code is returned to the caller once and
  is in no row, log or exception; ``repr`` of the result leaves it out.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth.accounts import new_invite_code
from ownvitals.auth.admin_accounts import AccountMissing

_SIGNOUT = text("SELECT outcome, sessions_revoked FROM public.admin_account_signout(:target)")
_INVITE = text("SELECT public.admin_invite_create(:code_hash, :expires_in)")


@dataclass(frozen=True, slots=True, repr=False)
class NewInvite:
    """An invite code to show once, and when it expires. ``repr`` omits the code."""

    code: str
    expires_at: datetime

    def __repr__(self) -> str:
        return f"{type(self).__name__}(expires_at={self.expires_at.isoformat()})"


async def sign_out_account(db: AsyncSession, user_id: UUID) -> int:
    """Delete every session of ``user_id`` in the admin transaction ``db`` (its ``ok`` row for
    ``sessions.revoke_all`` and ``user_id`` first). Returns how many went; raises
    ``AccountMissing`` (nothing changed) for an unknown account."""
    row = (await db.execute(_SIGNOUT, {"target": user_id})).one()
    if row.outcome == "not_found":
        raise AccountMissing()
    if row.outcome != "ok":
        raise RuntimeError("the admin sign-out function answered an unknown outcome")
    return int(row.sessions_revoked)


async def create_admin_invite(db: AsyncSession, expires_in: timedelta) -> NewInvite:
    """One invite in the admin transaction ``db`` (its ``ok`` row for ``invite.create`` first).
    Only the code's SHA-256 reaches the database."""
    code, digest = new_invite_code()
    expires_at: datetime = (
        await db.execute(_INVITE, {"code_hash": digest, "expires_in": expires_in})
    ).scalar_one()
    return NewInvite(code=code, expires_at=expires_at)
