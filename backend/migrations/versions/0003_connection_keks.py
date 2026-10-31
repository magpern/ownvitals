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


def downgrade() -> None:
    """Drops the check values: the next start records them again for the keys it then holds."""
    op.execute("DROP TABLE public.connection_keks")
