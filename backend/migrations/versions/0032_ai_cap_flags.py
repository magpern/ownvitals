"""admin: the AI monthly caps as stored flags (#528, ADR 0014 amendment of 2026-11-17)

Revision ID: 0032_ai_cap_flags
Revises: 0031_users_synthetic
Create Date: 2026-11-17

``app_settings_flag`` admits two more keys, ``ai_user_monthly_cap_microusd`` and
``ai_global_monthly_cap_microusd`` (``ownvitals.flags.core.KEYS``). Their values are integers in
micro-USD that can only lower the environment's caps of the same name (``ownvitals.flags.caps``);
nothing else changes: the privileges stay as 0006 set them (the app role reads, the admin area
writes as ``ownvitals_maint``) and no budget function changes, since the worker still passes the
caps on every ``ai_reserve``.

Downgrade: the two rows go first (the old check would refuse them), so the environment's caps
apply again, which is never higher than what was in force.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032_ai_cap_flags"
down_revision: str | Sequence[str] | None = "0031_users_synthetic"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = "'signup_mode', 'experimental_findings', 'ai_insights', 'banner'"
SETTING_KEYS = (
    "'signup_mode', 'experimental_findings', 'ai_insights', 'banner', "
    "'ai_user_monthly_cap_microusd', 'ai_global_monthly_cap_microusd'"
)


def _check(keys: str) -> None:
    op.execute("ALTER TABLE public.app_settings DROP CONSTRAINT app_settings_flag")
    op.execute(
        "ALTER TABLE public.app_settings ADD CONSTRAINT app_settings_flag "
        f"CHECK (flag IN ({keys}))"
    )


def upgrade() -> None:
    _check(SETTING_KEYS)


def downgrade() -> None:
    op.execute(
        "DELETE FROM public.app_settings WHERE flag IN "
        "('ai_user_monthly_cap_microusd', 'ai_global_monthly_cap_microusd')"
    )
    _check(BEFORE)
