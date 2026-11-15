"""``SessionInfo.full`` as a SQL condition on ``sessions`` (#475), for the queries that judge
sessions without resolving them: the purge, the live-session list and the disconnect job's
step-up re-check.

The same rule as ``SessionInfo.full``: an account with a confirmed TOTP owes nothing once
``mfa_at`` is set (its TOTP or recovery code passed in the session); an account without one owes
nothing when it is not required to have one (stored role ``user`` and the address not in
``OWNVITALS_ADMIN_EMAILS``; ``second_factor_policy.totp_required``), whatever ``mfa_at`` says.
"""

from collections.abc import Collection
from typing import Any

from sqlalchemy import and_, or_, select

from ownvitals.auth.tables import mfa_totp, sessions, users


def owes_no_second_factor(admin_emails: Collection[str]) -> Any:
    """The condition, correlated to ``sessions``."""
    has_totp = (
        select(mfa_totp.c.id)
        .where(mfa_totp.c.user_id == sessions.c.user_id, mfa_totp.c.confirmed_at.is_not(None))
        .exists()
    )
    not_required = (
        select(users.c.id)
        .where(
            users.c.id == sessions.c.user_id,
            users.c.role == "user",
            users.c.email_normalized.not_in(list(admin_emails)),
        )
        .exists()
    )
    return or_(and_(has_totp, sessions.c.mfa_at.is_not(None)), and_(~has_totp, not_required))
