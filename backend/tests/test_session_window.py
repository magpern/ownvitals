"""``SessionInfo`` window bounds (#140, #143).

A future moment is outside every window. For a session that is not full, a future
``reauth_at`` or ``stepup_at`` is not a recent proof, so it cannot hold the session open.
``stepup_within`` keeps the shorter of the asked window and the configured step-up window.

No database and no account data: the moments are fixed instants.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from ownvitals.auth.sessions import ADMIN_STEPUP_WINDOW, STEPUP_WINDOW, SessionInfo

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _info(
    *,
    reauth_at: datetime | None = NOW,
    mfa_at: datetime | None = NOW,
    stepup_at: datetime | None = NOW,
    stepup_window: timedelta = STEPUP_WINDOW,
    totp_enabled: bool = True,
) -> SessionInfo:
    return SessionInfo(
        id=uuid4(),
        user_id=uuid4(),
        email="admin@example.com",
        stored_role="admin",
        created_at=NOW,
        last_seen_at=NOW,
        expires_at=NOW + timedelta(days=30),
        reauth_at=reauth_at,
        mfa_at=mfa_at,
        stepup_at=stepup_at,
        now=NOW,
        totp_enabled=totp_enabled,
        stepup_window=stepup_window,
    )


def _pending(*, reauth_at: datetime | None, stepup_at: datetime | None) -> SessionInfo:
    """A session that is not full: no confirmed TOTP and no MFA time."""
    return _info(reauth_at=reauth_at, mfa_at=None, stepup_at=stepup_at, totp_enabled=False)


def test_a_moment_after_now_is_outside_every_window() -> None:
    future = NOW + timedelta(seconds=1)
    info = _info(reauth_at=future, mfa_at=future, stepup_at=future)
    assert info.recent_auth is False
    assert info.recent_mfa is False
    assert info.role == "user"
    assert info.admin_pending_mfa is True
    assert info.recent_stepup is False
    assert info.stepup_within(ADMIN_STEPUP_WINDOW) is False


def test_a_moment_at_now_or_at_the_window_edge_is_inside() -> None:
    fresh = _info()
    assert fresh.recent_auth is True
    assert fresh.recent_mfa is True
    assert fresh.role == "admin"
    assert fresh.recent_stepup is True
    assert fresh.stepup_within(ADMIN_STEPUP_WINDOW) is True
    on_edge = _info(stepup_at=NOW - ADMIN_STEPUP_WINDOW)
    assert on_edge.stepup_within(ADMIN_STEPUP_WINDOW) is True
    past_edge = _info(stepup_at=NOW - ADMIN_STEPUP_WINDOW - timedelta(seconds=1))
    assert past_edge.stepup_within(ADMIN_STEPUP_WINDOW) is False


def test_stepup_within_keeps_the_shorter_configured_window() -> None:
    """Four minutes is inside the five-minute admin cap and outside a two-minute setting.
    Dropping ``min`` and keeping only the asked window would accept it."""
    short = timedelta(minutes=2)
    between = _info(stepup_at=NOW - timedelta(minutes=4), stepup_window=short)
    assert between.stepup_within(ADMIN_STEPUP_WINDOW) is False
    inside = _info(stepup_at=NOW - timedelta(minutes=1), stepup_window=short)
    assert inside.stepup_within(ADMIN_STEPUP_WINDOW) is True


def test_a_future_proof_is_not_recent_for_pending_expiry() -> None:
    """``pending_expired`` only: a future proof is not recent.

    The session is not full. A ``reauth_at`` or ``stepup_at`` after ``now`` does not
    count, including when it is later than a proof already outside the ten minutes.
    A mutant that treats that future proof as recent keeps the session and fails here.
    A proof at ``now``, or exactly ten minutes ago, still counts.
    """
    future = NOW + timedelta(seconds=1)
    stale = NOW - timedelta(minutes=10, seconds=1)
    window = timedelta(minutes=10)

    for reauth_at, stepup_at in (
        (future, None),
        (None, future),
        (stale, future),
        (future, stale),
    ):
        pending = _pending(reauth_at=reauth_at, stepup_at=stepup_at)
        assert pending.full is False
        assert pending.pending_expired is True

    assert _pending(reauth_at=None, stepup_at=None).pending_expired is True
    assert _pending(reauth_at=NOW, stepup_at=future).pending_expired is False
    assert _pending(reauth_at=stale, stepup_at=NOW).pending_expired is False
    on_edge = _pending(reauth_at=NOW - window, stepup_at=None)
    assert on_edge.pending_expired is False
    past_edge = _pending(reauth_at=NOW - window - timedelta(seconds=1), stepup_at=None)
    assert past_edge.pending_expired is True
