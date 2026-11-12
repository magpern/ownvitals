"""Email preferences per mail kind, for the settings and the one-click unsubscribe (#459)

Revision ID: 0029_email_prefs
Revises: 0028_chart_range_7d
Create Date: 2026-11-12

Roadmap Amendment 7 ("M6 additions", classbeat #655) and ADR 0017: a user can switch off the
email of each suppressible outbox kind, in the settings or through the signed unsubscribe link
of a mail of that kind.

``email_prefs`` (owned): at most one row per owner and kind. No row means the kind's email is on
at generation 0. ``enabled`` is the choice; ``generation`` counts every change of it, and an
unsubscribe link binds the generation it was made at, so a link stops working once the choice
has changed (replay). The kinds are the suppressible ones only: ``export_ready`` and the account
and security mails can never be switched off, so the check refuses them. No value, address or
token is stored.

Web's settings route writes the caller's own row in their session (forced RLS). The unsubscribe
endpoint writes only through the outbox family's ``email_unsubscribe`` and web's sender reads
only through ``mail_outbox_email_pref`` (``docker/postgres/notify/50-outbox.sql``); this
migration creates no function.

Expand only: one new owned table (forced RLS, the ``owner_isolation`` policy), no change to an
existing one. The vocabulary is written out here on purpose; ``ownvitals.notify.email_policy``
holds the same list and ``tests/test_notify_email_prefs_db.py`` compares it with the check.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0029_email_prefs"
down_revision: str | Sequence[str] | None = "0028_chart_range_7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREFS = """
CREATE TABLE public.email_prefs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    kind text NOT NULL CONSTRAINT email_prefs_kind
        CHECK (kind IN ('findings_waiting', 'connection_attention', 'ai_digest_ready')),
    enabled boolean NOT NULL DEFAULT true,
    generation bigint NOT NULL DEFAULT 0 CONSTRAINT email_prefs_generation CHECK (generation >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT email_prefs_owner_kind UNIQUE (owner_id, kind),
    CONSTRAINT email_prefs_owner_id_id_key UNIQUE (owner_id, id)
)"""


def upgrade() -> None:
    op.execute(_PREFS)
    enable_owner_rls("email_prefs")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.email_prefs")
