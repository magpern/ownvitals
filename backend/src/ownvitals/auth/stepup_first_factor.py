"""Step-up by the first factor, for an account without TOTP (#475; ADR 0003 amendment of
2026-11-12).

The password re-authentication (``signin_routes``), the Google re-authentication
(``oauth_outcomes``) and the passkey re-authentication (``passkey_reauth_routes``) call this
inside their final transaction, after they took the user-row lock (``userlock``) and before they
rotate the session. It reads the account as committed now, so a TOTP confirmed or a role granted
meanwhile counts: such an account gets ``reauth_at`` only, as before #475, and must step up with
a TOTP or recovery code. The rule is ``second_factor_policy.first_factor_steps_up``.
"""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.auth import second_factor_policy
from ownvitals.auth.events import record_event
from ownvitals.auth.tables import mfa_totp, users
from ownvitals.config import Settings

Method = Literal["password", "google", "passkey"]


async def steps_up(db: AsyncSession, settings: Settings, user_id: UUID) -> bool:
    """Whether a fresh first factor of ``user_id`` also sets ``stepup_at``. The caller holds the
    user-row lock. ``False`` for an account that is gone (fails closed)."""
    row = (
        await db.execute(
            select(
                users.c.role,
                users.c.email_normalized,
                select(mfa_totp.c.id)
                .where(mfa_totp.c.user_id == users.c.id, mfa_totp.c.confirmed_at.is_not(None))
                .exists()
                .label("totp_enabled"),
            ).where(users.c.id == user_id)
        )
    ).first()
    if row is None:
        return False
    required = second_factor_policy.totp_required(
        row.role, row.email_normalized, settings.admin_email_set
    )
    return second_factor_policy.first_factor_steps_up(
        totp_enabled=row.totp_enabled, required=required
    )


async def record_stepup(db: AsyncSession, user_id: UUID, ip: str | None, method: Method) -> None:
    """The ``step_up`` row of a first-factor step-up (``detail.method``; the activity list keeps
    step-up bookkeeping out, as for a TOTP step-up)."""
    await record_event(db, "step_up", user_id=user_id, ip=ip, detail={"method": method})
