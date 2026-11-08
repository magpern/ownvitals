"""accounts: the coarse origin of a security event (#251)

Revision ID: 0018_auth_event_origin
Revises: 0017_account_locale
Create Date: 2026-11-08

``auth_events`` gains the browser family, the operating system and the place of the request
that wrote the row (``auth/event_origin.py``), so the user's own activity list can show where
an event came from without the address or the ``User-Agent`` string. All three are nullable: a
row written outside a request (the purge, the operator CLI) and every older row has none. The
checks bound the lengths as ``qr_logins`` does for the same values (0015_qr_login); the reader
keeps only its allowlisted names.

The index serves that list: one user's rows, newest first, paged by ``(at, id)``.

A system table (ADR 0002 section 3): no ``owner_id`` and no RLS, so no policy changes; the app
role's DML grants already cover a new column.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_auth_event_origin"
down_revision: str | Sequence[str] | None = "0017_account_locale"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = {
    "browser": "char_length(browser) <= 32",
    "os": "char_length(os) <= 32",
    "place": "char_length(place) BETWEEN 1 AND 64",
}


def upgrade() -> None:
    for column, check in COLUMNS.items():
        op.execute(
            f"ALTER TABLE public.auth_events ADD COLUMN {column} text "
            f"CONSTRAINT auth_events_{column} CHECK ({check})"
        )
    op.execute("CREATE INDEX auth_events_user_at_idx ON public.auth_events (user_id, at, id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.auth_events_user_at_idx")
    for column in COLUMNS:
        op.execute(f"ALTER TABLE public.auth_events DROP COLUMN IF EXISTS {column}")
