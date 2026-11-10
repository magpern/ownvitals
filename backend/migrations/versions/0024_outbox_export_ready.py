"""The outbox kind ``export_ready`` and the route ``settings_data`` (ADR 0013 section 4; #333)

Revision ID: 0024_outbox_export_ready
Revises: 0023_ai_audit
Create Date: 2026-11-09

ADR 0007's outbox gains one user-facing kind, ``export_ready``: the mail that says something the
user asked for is waiting, linking to Settings, "Your data" (``/settings/data``, the route key
``settings_data``). Never a download link, never a value (T59). The kinds and routes are
``ownvitals.notify.outbox``'s; ``tests/test_notify_outbox.py`` compares them with these checks.

Only the two CHECK constraints of ``mail_outbox`` change: no table, column or row is added. The
downgrade deletes the rows of the new kind or route first (they are mail not yet sent; the
export itself stays in Settings; forced row-level security is lifted for that one statement),
then restores the old checks.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_outbox_export_ready"
down_revision: str | Sequence[str] | None = "0023_ai_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'findings_waiting', 'connection_attention'"
_ROUTES = "'overview', 'findings', 'connections'"


def _checks(kinds: str, routes: str) -> None:
    op.execute(
        "ALTER TABLE public.mail_outbox "
        "DROP CONSTRAINT mail_outbox_kind, "
        f"ADD CONSTRAINT mail_outbox_kind CHECK (kind IN ({kinds})), "
        "DROP CONSTRAINT mail_outbox_route, "
        f"ADD CONSTRAINT mail_outbox_route CHECK (route IN ({routes}))"
    )


def upgrade() -> None:
    _checks(f"{_KINDS}, 'export_ready'", f"{_ROUTES}, 'settings_data'")


def downgrade() -> None:
    # The table is under forced row-level security, which would hide every row from this
    # session (no user): lifted for the one statement, as the owner, and forced again.
    op.execute("ALTER TABLE public.mail_outbox NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "DELETE FROM public.mail_outbox WHERE kind = 'export_ready' OR route = 'settings_data'"
    )
    op.execute("ALTER TABLE public.mail_outbox FORCE ROW LEVEL SECURITY")
    _checks(_KINDS, _ROUTES)
