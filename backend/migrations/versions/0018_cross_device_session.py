"""sessions: the cross-device passkey session (ADR 0011 section 3, amendment of 2026-11-08; #252)

Revision ID: 0018_cross_device_session
Revises: 0017_account_locale
Create Date: 2026-11-08

``sessions.origin`` gains a third value, ``passkey_cross_device``: a session born from a passkey
sign-in whose authenticator was not this device's own (a phone over the hybrid transport, a
security key, or an attachment claim the registered transports contradict). Its idle limit is
shorter (``session_model.CROSS_DEVICE_IDLE``) and its ``expires_at`` is capped at sign-in;
every rotation carries ``origin`` over, as for ``qr_approval``. No new column: the origin check
is widened. The QR checks (``sessions_qr_approval``, ``sessions_mfa_by_approval``) need no
change: a cross-device session has no approval factor.

``sessions`` is a system table (ADR 0002 section 3): no RLS, no policy change. The downgrade
deletes every cross-device session (fail closed: it can't keep its shorter idle limit) before
narrowing the check back.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_cross_device_session"
down_revision: str | Sequence[str] | None = "0017_account_locale"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _origin_check(values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return (
        "ALTER TABLE public.sessions DROP CONSTRAINT sessions_origin, "
        f"ADD CONSTRAINT sessions_origin CHECK (origin IN ({allowed}))"
    )


def upgrade() -> None:
    op.execute(_origin_check(("signin", "qr_approval", "passkey_cross_device")))


def downgrade() -> None:
    op.execute("DELETE FROM public.sessions WHERE origin = 'passkey_cross_device'")
    op.execute(_origin_check(("signin", "qr_approval")))
