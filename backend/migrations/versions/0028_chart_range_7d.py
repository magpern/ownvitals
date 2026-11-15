"""The 7 day chart range of the profile (issue 402)

Revision ID: 0028_chart_range_7d
Revises: 0027_ai_conversation_retention
Create Date: 2026-11-11

The owner added 7 days to the chart ranges and made it the default (2026-11-11). This widens the
``user_profiles_chart_range`` CHECK to ``('7d', '30d', '90d', '6mo', '1y')`` and sets the column
default to ``7d`` for rows created from now on; a saved value (``90d`` included) is untouched and
still wins. No privilege, policy or row changes: the table keeps its forced RLS (security-adjacent:
it is the profile store).

The downgrade maps ``7d`` rows to ``30d`` (the nearest shorter list entry), with RLS un-forced for
that one statement, restores the narrower CHECK and the ``90d`` default.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_chart_range_7d"
down_revision: str | Sequence[str] | None = "0027_ai_conversation_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "user_profiles_chart_range"


def _set(values: str, default: str) -> None:
    op.execute(f"ALTER TABLE public.user_profiles DROP CONSTRAINT {_CHECK}")
    op.execute(
        f"ALTER TABLE public.user_profiles ADD CONSTRAINT {_CHECK} "
        f"CHECK (chart_range IN ({values}))"
    )
    op.execute(f"ALTER TABLE public.user_profiles ALTER COLUMN chart_range SET DEFAULT '{default}'")


def upgrade() -> None:
    _set("'7d', '30d', '90d', '6mo', '1y'", "7d")


def downgrade() -> None:
    op.execute("ALTER TABLE public.user_profiles NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE public.user_profiles SET chart_range = '30d' WHERE chart_range = '7d'")
    op.execute("ALTER TABLE public.user_profiles FORCE ROW LEVEL SECURITY")
    _set("'30d', '90d', '6mo', '1y'", "90d")
