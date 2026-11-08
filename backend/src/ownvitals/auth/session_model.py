"""The session model (ADR 0003 sections 2 and 3): what a resolved session is and the
windows its checks use. Pure: no database, no clock (``now`` is the database's, passed in).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

ADMIN_MFA_WINDOW = timedelta(hours=12)
RECENT_AUTH_WINDOW = timedelta(minutes=10)
# The step-up window is the setting ``stepup_window_minutes`` (PLAN section 6: 10 minutes at
# most); admin writes use their own 5 minutes (ADR 0003 section 4).
STEPUP_WINDOW = timedelta(minutes=10)
ADMIN_STEPUP_WINDOW = timedelta(minutes=5)
# A session that has not passed its second factor lives this long after its latest proof (the
# sign-in, or the step-up that removed the factor), then it is deleted (ADR 0003 section 2).
PENDING_WINDOW = RECENT_AUTH_WINDOW
# A passkey sign-in from another device (ADR 0011 section 3, amendment of 2026-11-08, #252): a
# session that may sit on a shared computer. Its absolute and idle limits are these, or the
# settings' if those are shorter; ``origin`` marks it for its whole life (every rotation).
CROSS_DEVICE = "passkey_cross_device"
CROSS_DEVICE_ABSOLUTE = timedelta(hours=24)
CROSS_DEVICE_IDLE = timedelta(hours=1)
Origin = Literal["signin", "qr_approval", "passkey_cross_device"]


def idle_limit(configured: timedelta, origin: str) -> timedelta:
    """The idle limit of a session born as ``origin``: ``configured`` (the setting), or at most
    ``CROSS_DEVICE_IDLE`` for a cross-device passkey session."""
    return min(configured, CROSS_DEVICE_IDLE) if origin == CROSS_DEVICE else configured


class SessionGone(Exception):
    """The session to rotate no longer exists (revoked or rotated by a parallel request)."""


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """A valid session joined with its user, as of the database time ``now``."""

    id: UUID
    user_id: UUID
    email: str
    stored_role: Literal["user", "admin"]
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    reauth_at: datetime | None
    mfa_at: datetime | None
    stepup_at: datetime | None
    now: datetime
    totp_enabled: bool = False  # the account has a confirmed TOTP
    stepup_window: timedelta = STEPUP_WINDOW
    # ADR 0011 section 5: ``qr_approval`` for a session a phone approved (carried over every
    # rotation), and whether ``mfa_at`` is still the code entered on that phone. Section 3's
    # 2026-11-08 amendment adds ``passkey_cross_device`` (``CROSS_DEVICE``).
    origin: Origin = "signin"
    mfa_by_approval: bool = False

    @property
    def full(self) -> bool:
        """The session passed its second factor (ownvitals ADR 0003 section 2): the account has
        a confirmed TOTP and a TOTP or recovery code was verified in this session (or its first
        enrolment was confirmed in it), or on its behalf for the one QR approval that created
        it (ADR 0011 section 5, amending ADR 0003 section 2). Only a full session yields a
        ``UserContext``."""
        return self.totp_enabled and self.mfa_at is not None

    @property
    def second_factor(self) -> Literal["enrol", "verify"] | None:
        """What a session that is not full still owes: ``enrol`` (no TOTP yet) or ``verify``."""
        if self.full:
            return None
        return "verify" if self.totp_enabled else "enrol"

    @property
    def pending_expired(self) -> bool:
        """A session that is not full and has no proof inside the ten-minute window.

        Only a ``reauth_at`` or ``stepup_at`` whose age is inside ``[0, PENDING_WINDOW]``
        counts. A timestamp after ``now`` is not a recent proof, so it cannot hold the
        session open.
        """
        if self.full:
            return False
        return not (
            _within(self.reauth_at, self.now, PENDING_WINDOW)
            or _within(self.stepup_at, self.now, PENDING_WINDOW)
        )

    @property
    def role(self) -> Literal["user", "admin"]:
        """``admin`` only for an admin whose session passed TOTP in the last 12 hours, in this
        session itself: a code entered on the approving phone for a QR login does not count
        (ADR 0011 section 5, ADR 0003 section 7 read literally)."""
        if (
            self.stored_role == "admin"
            and not self.mfa_by_approval
            and _within(self.mfa_at, self.now, ADMIN_MFA_WINDOW)
        ):
            return "admin"
        return "user"

    @property
    def may_approve_qr(self) -> bool:
        """A full session that was not itself born from a QR approval (no chaining from a
        hijacked desktop, ADR 0011 section 5) nor from a cross-device passkey sign-in (a
        possibly shared computer, section 3's 2026-11-08 amendment)."""
        return self.full and self.origin == "signin"

    @property
    def admin_pending_mfa(self) -> bool:
        """An admin account whose TOTP check is missing or older than 12 hours (``role`` is
        ``user`` meanwhile). False for every other account."""
        return self.stored_role == "admin" and self.role != "admin"

    @property
    def recent_mfa(self) -> bool:
        """TOTP passed within the 12-hour window (in this session: see ``role``)."""
        return not self.mfa_by_approval and _within(self.mfa_at, self.now, ADMIN_MFA_WINDOW)

    @property
    def recent_auth(self) -> bool:
        return _within(self.reauth_at, self.now, RECENT_AUTH_WINDOW)

    @property
    def recent_stepup(self) -> bool:
        """A TOTP or recovery code within the step-up window (``stepup_window_minutes``)."""
        return _within(self.stepup_at, self.now, self.stepup_window)

    def stepup_within(self, window: timedelta) -> bool:
        """A TOTP or recovery code within ``window`` and within the step-up window."""
        return _within(self.stepup_at, self.now, min(window, self.stepup_window))


def _within(moment: datetime | None, now: datetime, window: timedelta) -> bool:
    """True when ``moment`` is present and its age is inside ``[0, window]``.

    A moment after ``now`` (a future ``stepup_at`` or ``mfa_at``) is outside: the lower bound
    is ``timedelta(0)``, so a negative age fails closed.
    """
    return moment is not None and timedelta(0) <= now - moment <= window


@dataclass(frozen=True, slots=True)
class Resolution:
    """``anonymous``: no cookie or an unknown token (the cookie is left alone); ``refused``: a
    known session that expired, idled out or whose user is disabled (deleted; clear the cookie);
    ``ok``: ``info`` is set."""

    outcome: Literal["anonymous", "refused", "ok"]
    info: SessionInfo | None = None


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A new session. ``token`` goes into the cookie once and is never stored or logged."""

    id: UUID
    token: str
    expires_at: datetime
    max_age: int  # remaining absolute lifetime, in seconds
