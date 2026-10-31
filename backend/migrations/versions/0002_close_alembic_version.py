"""close alembic_version to the app role

Revision ID: 0002_close_alembic_version
Revises: 0001_baseline
Create Date: 2026-10-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_close_alembic_version"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Alembic's own bookkeeping table is closed to the app role (ADR 0002 sections 3 and 5).

    The owner's default privileges gave ``ownvitals_app`` DML on ``alembic_version`` when Alembic
    created it; the app must not be able to change migration state. The baseline could not say
    so: the roles (``docker/postgres/init/10-roles.sql``) did not exist yet.
    """
    op.execute("REVOKE ALL ON TABLE public.alembic_version FROM ownvitals_app, PUBLIC")


def downgrade() -> None:
    """Nothing to undo: the table stays closed to the app role (reopening it would only widen
    what the app may do)."""
