"""accounts: ``users.synthetic``, the mark of the AI evaluation's seeded accounts (#531, ADR 0014 §9)

Revision ID: 0031_users_synthetic
Revises: 0030_session_anomaly
Create Date: 2026-11-15

ADR 0014 section 9: the model evals run on their own database, whose name ends in ``_ai_eval``,
and seed their own synthetic accounts, "each with ``users.synthetic = true``, a column only
seeders set". Before every model call the evaluation checks that every account there is marked; one
unmarked account and it stops.

- ``synthetic boolean NOT NULL DEFAULT false``: every existing and every signed-up account is
  unmarked.
- The app role keeps SELECT and DELETE on ``users`` but loses table-level INSERT and UPDATE; it
  gets both back on every column **except** ``synthetic``. So web, the worker and the operator
  CLI (all ``ownvitals_app``) can neither create a marked account nor mark or unmark one, in any
  database. Only the table's owner, ``ownvitals_owner`` (the migration role), writes the column:
  the evaluation seeder (``ownvitals.auth.synthetic``) inserts its accounts on that role's connection,
  and refuses unless the database's name ends in ``_ai_eval``.
- A later migration that adds a ``users`` column grants the app role INSERT and UPDATE on it in
  the same migration (``tests/role_guard.py`` expects them on every column but ``synthetic``);
  ``docker/postgres/init/10-roles.sql`` restores the same grants after its blanket re-grant.

System table (ADR 0002 section 3): no ``owner_id`` and no RLS, so no policy changes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031_users_synthetic"
down_revision: str | Sequence[str] | None = "0030_session_anomaly"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every column of users at this revision but ``synthetic``.
APP_WRITABLE = (
    "id",
    "email",
    "email_normalized",
    "email_verified_at",
    "password_hash",
    "role",
    "disabled_at",
    "created_at",
    "webauthn_handle",
    "locale",
)
_COLUMNS = ", ".join(APP_WRITABLE)


def upgrade() -> None:
    op.execute("ALTER TABLE public.users ADD COLUMN synthetic boolean NOT NULL DEFAULT false")
    # A table-level REVOKE also drops the column grants, so the column grants follow it.
    op.execute("REVOKE INSERT, UPDATE ON TABLE public.users FROM ownvitals_app")
    op.execute(
        f"GRANT INSERT ({_COLUMNS}), UPDATE ({_COLUMNS}) ON TABLE public.users TO ownvitals_app"
    )


def downgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE ON TABLE public.users FROM ownvitals_app")
    op.execute("GRANT INSERT, UPDATE ON TABLE public.users TO ownvitals_app")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS synthetic")
