"""notification hold: one row per E4/E6 hold and how it ended (#195)

Revision ID: 0020_notify_holds
Revises: 0019_auth_event_origin
Create Date: 2026-11-09

ADR 0006 section 11 ("E4 and E6 notifications wait for the gate") and its test specification:
an E4 or E6 notification waits while a trigger group can still be linked or a linked recording
lacks a current ``estimated``, at most a bound after the finding's first revision, and the hold
records how it ended as a reason code (for A9 / OC5's late-link count).

``notify_holds`` (owned): one row per finding that was held, ``UNIQUE (owner_id, finding_id)``.
``finding_id`` is a plain column, not a foreign key: a finding withdrawn during its hold is
deleted by the rules engine, and its hold must stay to record ``withdrawn``. ``started_at`` is
the first revision's start (the bound counts from it), ``revision_at`` the current revision's
(a new revision restarts the hold; an ``ecg`` sync counts only when it started after it),
``revision_seen`` and ``revision_groups`` the revision's ``last_seen`` and trigger groups, which
tell a new revision from the same one. ``ended_at``, ``reason`` and ``outcome`` are set together,
once. No value, finding text or evidence is stored.

Expand only: one new owned table (forced RLS, the ``owner_isolation`` policy), no change to an
existing one, no data moved. The vocabularies are written out here on purpose;
``ownvitals.notify.tables`` holds the same lists and ``tests/test_notify_hold_db.py`` compares
them with the check constraints.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0020_notify_holds"
down_revision: str | Sequence[str] | None = "0019_auth_event_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HOLDS = """
CREATE TABLE public.notify_holds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    finding_id uuid NOT NULL,
    rule_id text NOT NULL CONSTRAINT notify_holds_rule CHECK (rule_id IN ('E4', 'E6')),
    started_at timestamptz NOT NULL,
    revision_at timestamptz NOT NULL,
    revision_seen timestamptz NOT NULL,
    revision_groups bigint[] NOT NULL DEFAULT '{}'::bigint[]
        CONSTRAINT notify_holds_groups CHECK (array_position(revision_groups, NULL) IS NULL),
    ended_at timestamptz,
    reason text CONSTRAINT notify_holds_reason CHECK (reason IN ('linked',
        'capability_unsupported', 'ecg_sync_no_match', 'no_trigger', 'timeout', 'withdrawn')),
    outcome text CONSTRAINT notify_holds_outcome CHECK (outcome IN ('sent',
        'sent_not_evaluated', 'hidden', 'none')),
    CONSTRAINT notify_holds_revision CHECK (revision_at >= started_at),
    CONSTRAINT notify_holds_end CHECK (
        (ended_at IS NULL) = (reason IS NULL) AND (ended_at IS NULL) = (outcome IS NULL)
        AND (ended_at IS NULL OR ended_at >= started_at)),
    CONSTRAINT notify_holds_owner_id_id_key UNIQUE (owner_id, id),
    CONSTRAINT notify_holds_finding_key UNIQUE (owner_id, finding_id)
)"""

# The notification step's scan: one owner's open holds.
_OPEN = "CREATE INDEX notify_holds_open ON public.notify_holds (owner_id) WHERE ended_at IS NULL"


def upgrade() -> None:
    op.execute(_HOLDS)
    op.execute(_OPEN)
    enable_owner_rls("notify_holds")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.notify_holds")
