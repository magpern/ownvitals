"""``SessionInfo`` window bounds (#140): a future moment is outside, and ``stepup_within``
keeps the shorter of the asked window and the configured step-up window.

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
        totp_enabled=True,
        stepup_window=stepup_window,
    )


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
