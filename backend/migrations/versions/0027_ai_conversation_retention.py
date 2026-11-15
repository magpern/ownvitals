"""The conversation retention purge's index (ADR 0014 section 5; #395)

Revision ID: 0027_ai_conversation_retention
Revises: 0026_ai_digest
Create Date: 2026-11-11

The worker's hourly purge deletes the ``ai_conversations`` last updated more than
``ai_conversation_retention_days`` days ago, oldest first and a bounded batch at a time, through
the definer function ``ai_purge_expired`` of ``docker/postgres/ai/50-ai.sql`` (migrations never
create those functions). This index on ``(updated_at, id)`` serves that ordered, limited scan so
it does not read every user's conversations each hour.

Expand only: one index, no row or privilege changes. The downgrade drops it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_ai_conversation_retention"
down_revision: str | Sequence[str] | None = "0026_ai_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ai_conversations_updated "
        "ON public.ai_conversations (updated_at, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ai_conversations_updated")
