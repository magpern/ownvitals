"""mail outbox and owner notices (ADR 0007 section 1; #194)

Revision ID: 0011_outbox
Revises: 0010_withings_rate
Create Date: 2026-11-03

Two tables under the same rule (ADR 0007 section 1): a row holds a fixed ``kind``, its recipient
and a fixed route key, never an address, a subject, free text, a value or finding text. The
other columns are the send state the definer functions keep (``docker/postgres/notify/
50-outbox.sql``).

- ``mail_outbox`` is owned (mail to one user): ``UNIQUE (owner_id, id)`` and forced row-level
  security (ADR 0002 section 2), so a job writes only its owner's rows. Its kinds and routes are
  the user-facing ones. At most one pending row per owner and kind.
- ``owner_notices`` is a system table (mail to the admins): its ``kind`` admits only owner-notice
  kinds (amended #78), and the app role has no privilege on it (amended #72): it is reached only
  through the definer functions. At most one pending row per kind (#83 finding 1).

The kinds and routes are ``ownvitals.notify.outbox``'s; the test of this migration holds both
lists to the constraints. The functions and their role are not created here: that needs the
superuser (``db-functions``). This migration moves no data and bypasses no row-level security.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0011_outbox"
down_revision: str | Sequence[str] | None = "0010_withings_rate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE = "state IN ('pending', 'claimed', 'sent', 'failed')"
_PENDING = "state IN ('pending', 'claimed')"

_MAIL_OUTBOX = f"""
    CREATE TABLE public.mail_outbox (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
        kind text NOT NULL,
        route text NOT NULL,
        state text NOT NULL DEFAULT 'pending',
        attempts integer NOT NULL DEFAULT 0,
        hold_until timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        sent_at timestamptz,
        CONSTRAINT mail_outbox_owner_id_id_key UNIQUE (owner_id, id),
        CONSTRAINT mail_outbox_kind CHECK (kind IN ('findings_waiting', 'connection_attention')),
        CONSTRAINT mail_outbox_route CHECK (route IN ('overview', 'findings', 'connections')),
        CONSTRAINT mail_outbox_state CHECK ({_STATE}),
        CONSTRAINT mail_outbox_attempts CHECK (attempts BETWEEN 0 AND 5)
    )
"""

_OWNER_NOTICES = f"""
    CREATE TABLE public.owner_notices (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        kind text NOT NULL,
        route text NOT NULL,
        state text NOT NULL DEFAULT 'pending',
        attempts integer NOT NULL DEFAULT 0,
        hold_until timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        sent_at timestamptz,
        CONSTRAINT owner_notices_kind CHECK (kind IN ('connection_needs_reconnect')),
        CONSTRAINT owner_notices_route CHECK (route IN ('admin')),
        CONSTRAINT owner_notices_state CHECK ({_STATE}),
        CONSTRAINT owner_notices_attempts CHECK (attempts BETWEEN 0 AND 5)
    )
"""

_STATEMENTS = (
    _MAIL_OUTBOX,
    "CREATE UNIQUE INDEX mail_outbox_one_pending ON public.mail_outbox (owner_id, kind) "
    f"WHERE {_PENDING}",
    "CREATE INDEX mail_outbox_state_idx ON public.mail_outbox (state, created_at)",
    _OWNER_NOTICES,
    "CREATE UNIQUE INDEX owner_notices_one_pending ON public.owner_notices (kind) "
    f"WHERE {_PENDING}",
    # The system table is closed to the app role (ADR 0007 section 3, amended #72).
    "REVOKE ALL ON TABLE public.owner_notices FROM ownvitals_app, PUBLIC",
)


def upgrade() -> None:
    for statement in _STATEMENTS[:3]:
        op.execute(statement)
    enable_owner_rls("mail_outbox")
    for statement in _STATEMENTS[3:]:
        op.execute(statement)


def downgrade() -> None:
    # The functions of 50-outbox.sql are owned by their own role, which only the superuser can
    # drop (the tests drop them first, dbtools.ready_to_downgrade); they hold no dependency on
    # these tables.
    op.execute("DROP TABLE IF EXISTS public.owner_notices")
    op.execute("DROP TABLE IF EXISTS public.mail_outbox")
