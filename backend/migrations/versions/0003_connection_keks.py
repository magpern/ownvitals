"""the key ring's check table

Revision ID: 0003_connection_keks
Revises: 0002_close_alembic_version
Create Date: 2026-10-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_connection_keks"
down_revision: str | Sequence[str] | None = "0002_close_alembic_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The id patterns of the ring's three kinds of key (``ownvitals.crypto.envelope.KeyKind``).
KEY_ID = "'^(kek|seed|pep)-[a-z0-9-]{1,28}$'"

CONNECTION_KEKS = f"""
    CREATE TABLE public.connection_keks (
        key_id text PRIMARY KEY CONSTRAINT connection_keks_key_id CHECK (key_id ~ {KEY_ID}),
        check_value bytea NOT NULL
            CONSTRAINT connection_keks_check_value CHECK (octet_length(check_value) = 16),
        created_at timestamptz NOT NULL DEFAULT now()
    )"""


def upgrade() -> None:
    """``connection_keks`` (ADR 0004 sections 3 and 4): a system table holding the check value of
    each key id of the ring (KEKs, webhook seeds and peppers), reached only through
    ``ownvitals.crypto.keks``.

    The app role records a check value and reads it, never changes or forgets one: it gets
    ``SELECT`` and ``INSERT`` only, granted explicitly rather than trimmed from the owner's
    default privileges (``10-roles.sql`` restores the same grant when it runs again). Deleting
    the rows is ``keys wipe``'s, as the owner role (#13).
    """
    op.execute(CONNECTION_KEKS)
    op.execute("REVOKE ALL ON TABLE public.connection_keks FROM ownvitals_app, PUBLIC")
    op.execute("GRANT SELECT, INSERT ON TABLE public.connection_keks TO ownvitals_app")


REFUSE_WHILE_ROWS = """
    DO $$ BEGIN
      IF EXISTS (SELECT FROM public.connection_keks) THEN
        RAISE EXCEPTION 'connection_keks holds check rows; downgrade refused';
      END IF;
    END $$"""


def downgrade() -> None:
    """Refused while ``connection_keks`` holds a row. A start records the check rows of the
    **current** keys only, so the rows of retired KEKs (and seeds and peppers) could never be
    recreated, and every value still under a retired KEK would stay ``key_unavailable``. An
    empty table (a database no web or worker ever started on, or after ``keys wipe``, #13) is
    dropped."""
    op.execute(REFUSE_WHILE_ROWS)
    op.execute("DROP TABLE public.connection_keks")
