"""baseline

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-31
"""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No tables yet: the issues that need tables add them as the next revisions.

    The database roles do not exist in this migration's time (the roles script arrives with the
    tenant-isolation issue), so nothing here refers to them. That issue closes Alembic's own
    ``alembic_version`` table to the app role in the revision it adds.
    """


def downgrade() -> None:
    """Nothing to undo."""
