"""The flags as one request sees them: the environment joined with the stored flags (ADR 0003
section 6). Read per request; nothing is cached.

#10 ports the sign-up mode only. The stored flags (``app_settings``, a system table the admin
area writes) arrive with #12, which adds the read here; until then nothing is stored, so the
environment's mode applies. The signature already takes the session, so the callers do not
change when the read arrives.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.flags import core
from ownvitals.flags.core import SignupMode


async def signup_mode(db: AsyncSession, environment: SignupMode) -> SignupMode:
    """The stricter of ``OWNVITALS_SIGNUP_MODE`` and the stored ``signup_mode`` (none until
    #12)."""
    del db  # #12 reads the stored flag through it
    return core.stricter_signup(environment, None)
