"""Who must have an authenticator app, and when the first factor is also the step-up.

The owner's decision of 2026-11-12 (roadmap Amendment 13, ADR 0003 amendment of 2026-11-12, #475):
TOTP is optional for users and mandatory for admins. Pure: the caller reads the account and
passes it in.

- **Required:** an account whose stored role is ``admin``, or whose normalized address is in
  ``OWNVITALS_ADMIN_EMAILS`` (the next sign-in would make it an admin). Such an account is never
  full without a confirmed TOTP verified in the session (``SessionInfo.full``).
- **First factor as step-up:** only for an account with no confirmed TOTP that is not required
  to have one. A fresh password, Google re-authentication or passkey then sets ``stepup_at`` as
  well as ``reauth_at``. An account with a confirmed TOTP steps up with a TOTP or recovery code
  only, exactly as before.
"""

from collections.abc import Container


def totp_required(stored_role: str, email_normalized: str, admin_emails: Container[str]) -> bool:
    """Whether the account must have a confirmed TOTP before its session is full."""
    return stored_role == "admin" or email_normalized in admin_emails


def first_factor_steps_up(*, totp_enabled: bool, required: bool) -> bool:
    """Whether a fresh first factor (password, Google re-auth, passkey) also counts as step-up."""
    return not totp_enabled and not required
