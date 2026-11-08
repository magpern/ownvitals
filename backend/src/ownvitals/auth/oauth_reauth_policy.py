"""When Google's answer to a ``reauth`` flow counts as recent authentication (ADR 0003 section 1,
amendment of 2026-11-08, #247; ported from classbeat ADR 0003, #522 and #687).

Google ignores ``max_age=0`` and the OIDC ``claims`` request for ``auth_time``: its ID token
carries no fresh ``auth_time`` (classbeat's live check). So:

- an account **with a password** never re-authenticates through Google: it confirms with its
  password (or a passkey), whatever ``auth_time`` Google sends (``reauth_use_password``);
- an account **without a password** is re-authenticated by Google's answer to *this* flow, without
  a fresh ``auth_time``: the flow started at most ``REAUTH_MAX_AGE`` seconds ago by the database
  clock (``reauth_expired``), Google vouches for the email (``invalid``), and an ``auth_time`` that
  is present lies at most ``AUTH_TIME_SKEW`` seconds in the future (``reauth_stale``).

The caller has already checked state, PKCE, the nonce and the ID token (``iss``, ``aud``,
``exp``, ``iat``), that the session belongs to the flow's user and that the subject is that
user's Google identity. A recent authentication never counts as a step-up: every action that
needs one also needs a fresh TOTP (ADR 0003 section 4), which this rule does not touch.

Pure: no database, clock or network; ``now`` and the flow's age are passed in.
"""

from dataclasses import dataclass

GOOGLE = "google"  # ``oauth_providers.GOOGLE``, not imported: that module reads the settings
REAUTH_MAX_AGE = 300  # seconds from the flow's start to its callback
AUTH_TIME_SKEW = 60  # seconds an auth_time may lie in the future


@dataclass(frozen=True, slots=True)
class ReauthAnswer:
    provider: str
    has_password: bool  # read under the account's row lock
    flow_age: float | None  # seconds since the start, by the database clock; None: unknown
    email_verified: bool
    auth_time: int | None
    now: float


def refusal(answer: ReauthAnswer) -> tuple[str, str] | None:
    """``(outcome, why)`` when the answer is not a re-authentication, else ``None``. Every
    unknown fails closed."""
    if answer.provider != GOOGLE:
        return "invalid", "provider_unsupported"
    if answer.has_password:
        return "reauth_use_password", "has_password"
    age = answer.flow_age
    if age is None or not 0 <= age <= REAUTH_MAX_AGE:
        return "reauth_expired", "flow_old"
    if not answer.email_verified:
        return "invalid", "email_unverified"
    if answer.auth_time is not None and answer.auth_time - answer.now > AUTH_TIME_SKEW:
        return "reauth_stale", "auth_time_future"
    return None
