"""AI cost cap: reservations, per-user and global monthly spend (ADR 0014 section 7; #205)

Revision ID: 0013_ai_budget
Revises: 0012_outbox
Create Date: 2026-11-03

Units are integer micro-USD; a month is the calendar month in UTC, stored as its first day.

- ``ai_reservations`` (owned): one row per bounded model request: the amount reserved, the
  actual charge once settled, the state (``reserved``, ``settled``, ``released``), the job and
  the job's fencing token at reservation time.
- ``ai_spend`` (owned): one row per owner and month, ``reserved`` and ``spent``.
- ``ai_spend_global`` (system): one row per month, ``reserved`` and ``spent`` of every user.

Every counter and amount has a ``CHECK (... >= 0)``: the checks are against negatives, never
against the cap (an actual above its reservation is charged in full, ADR 0014 section 7).

Privileges: ``ownvitals_app`` keeps only ``SELECT`` on the two owned tables (under forced RLS,
to show a user their budget) and has no privilege at all on ``ai_spend_global``. Every write
goes through the definer functions of ``docker/postgres/ai/50-ai.sql``, which this migration
does not create (ADR 0005 section 3: never a migration). ``reservation.job_id`` is a plain
column, not a foreign key: a job row may go while its reservation must stay, and the reaper
(``ai_settle_lost``) settles a reservation whose job is gone. This migration moves no data.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0013_ai_budget"
down_revision: str | Sequence[str] | None = "0012_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONTH = "CHECK (extract(day FROM month) = 1)"

_RESERVATIONS = f"""
CREATE TABLE public.ai_reservations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    month date NOT NULL CONSTRAINT ai_reservations_month {_MONTH},
    amount bigint NOT NULL CONSTRAINT ai_reservations_amount CHECK (amount >= 0),
    actual bigint CONSTRAINT ai_reservations_actual CHECK (actual >= 0),
    state text NOT NULL DEFAULT 'reserved'
        CONSTRAINT ai_reservations_state CHECK (state IN ('reserved', 'settled', 'released')),
    job_id uuid NOT NULL,
    lease_version bigint NOT NULL
        CONSTRAINT ai_reservations_lease_version CHECK (lease_version >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    CONSTRAINT ai_reservations_settled CHECK ((state = 'settled') = (actual IS NOT NULL)),
    CONSTRAINT ai_reservations_ended CHECK ((state = 'reserved') = (ended_at IS NULL)),
    CONSTRAINT ai_reservations_owner_id_id_key UNIQUE (owner_id, id)
)"""

_SPEND = f"""
CREATE TABLE public.ai_spend (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    month date NOT NULL CONSTRAINT ai_spend_month {_MONTH},
    reserved bigint NOT NULL DEFAULT 0 CONSTRAINT ai_spend_reserved CHECK (reserved >= 0),
    spent bigint NOT NULL DEFAULT 0 CONSTRAINT ai_spend_spent CHECK (spent >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ai_spend_owner_id_id_key UNIQUE (owner_id, id),
    CONSTRAINT ai_spend_owner_id_month_key UNIQUE (owner_id, month)
)"""

_GLOBAL = f"""
CREATE TABLE public.ai_spend_global (
    month date PRIMARY KEY CONSTRAINT ai_spend_global_month {_MONTH},
    reserved bigint NOT NULL DEFAULT 0 CONSTRAINT ai_spend_global_reserved CHECK (reserved >= 0),
    spent bigint NOT NULL DEFAULT 0 CONSTRAINT ai_spend_global_spent CHECK (spent >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
)"""

_INDEXES = (
    # The reaper's scan: only open reservations.
    "CREATE INDEX ai_reservations_open ON public.ai_reservations (month, owner_id) "
    "WHERE state = 'reserved'",
    "CREATE INDEX ai_reservations_owner_month ON public.ai_reservations (owner_id, month)",
)

# The app role reads its own rows (forced RLS) and writes nothing; the global row is closed.
_GRANTS = (
    "REVOKE ALL ON TABLE public.ai_reservations FROM ownvitals_app, PUBLIC",
    "GRANT SELECT ON TABLE public.ai_reservations TO ownvitals_app",
    "REVOKE ALL ON TABLE public.ai_spend FROM ownvitals_app, PUBLIC",
    "GRANT SELECT ON TABLE public.ai_spend TO ownvitals_app",
    "REVOKE ALL ON TABLE public.ai_spend_global FROM ownvitals_app, PUBLIC",
)


def upgrade() -> None:
    for statement in (_RESERVATIONS, _SPEND, _GLOBAL, *_INDEXES):
        op.execute(statement)
    enable_owner_rls("ai_reservations")
    enable_owner_rls("ai_spend")
    for statement in _GRANTS:
        op.execute(statement)


def downgrade() -> None:
    # The definer functions of 50-ai.sql belong to ownvitals_ai, which the owner role cannot
    # drop; they reference these tables by name only, so dropping the tables leaves them
    # unusable until the next upgrade and db-functions run.
    op.execute("DROP TABLE IF EXISTS public.ai_spend_global")
    op.execute("DROP TABLE IF EXISTS public.ai_spend")
    op.execute("DROP TABLE IF EXISTS public.ai_reservations")
