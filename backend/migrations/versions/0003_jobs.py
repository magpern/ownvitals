"""jobs and job events (ADR 0005 section 1)

Revision ID: 0003_jobs
Revises: 0002_close_alembic_version
Create Date: 2026-10-31

Both tables are owned (ADR 0002 section 2): ``owner_id``, ``UNIQUE (owner_id, id)``, an
owner-inclusive foreign key from the events to their job, every other unique constraint including
``owner_id``, and ``enable_owner_rls``. ``owner_id`` has no foreign key to ``users`` yet: that
table comes with the accounts issue (#10), whose migration adds it.

``job_events`` is append-only: the restrictive ``events_no_update`` and ``events_no_delete``
policies refuse every update and delete (RLS is forced, so the owner role too); deleting a job (or,
from #10, an account) still removes its events, because referential actions run without row
security.

The queue's ``SECURITY DEFINER`` functions are not created here: the superuser script
``docker/postgres/queue/20-queue.sql`` owns them (ADR 0005 section 3). A later migration that
changes a column they read bumps the script's version in the same PR.

The default privileges of ``ownvitals_owner`` give ``ownvitals_app`` DML on both tables. This
migration moves no data.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0003_jobs"
down_revision: str | Sequence[str] | None = "0002_close_alembic_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = "'queued', 'running', 'completed', 'failed', 'cancelled'"
TERMINAL = "'completed', 'failed', 'cancelled'"
# ADR 0005 section 6; the same list as ``ownvitals.jobs.errors.CATEGORIES``.
CATEGORIES = (
    "'upstream_unavailable', 'rate_limited', 'lease_lost', 'not_connected', 'needs_reconnect', "
    "'key_unavailable', 'decrypt_failed', 'invalid_input', 'account_disabled', "
    "'outbound_blocked', 'internal'"
)
# ADR 0005 section 7; the same list as ``ownvitals.jobs.events.MODELS``.
EVENT_KINDS = (
    "'queued', 'claimed', 'progress', 'retry_scheduled', 'cancel_requested', 'cancelled', "
    "'completed', 'failed', 'requeued'"
)

JOBS = f"""
    CREATE TABLE public.jobs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL,
        kind text NOT NULL CONSTRAINT jobs_kind CHECK (kind ~ '^[a-z_]{{1,40}}$'),
        state text NOT NULL CONSTRAINT jobs_state CHECK (state IN ({STATES})),
        input jsonb NOT NULL CONSTRAINT jobs_input CHECK (octet_length(input::text) <= 16384),
        idempotency_key text NOT NULL
            CONSTRAINT jobs_idempotency_key CHECK (idempotency_key ~ '^[A-Za-z0-9_-]{{16,64}}$'),
        request_hash bytea NOT NULL
            CONSTRAINT jobs_request_hash CHECK (octet_length(request_hash) = 32),
        attempt integer NOT NULL DEFAULT 0,
        max_attempts integer NOT NULL DEFAULT 3
            CONSTRAINT jobs_max_attempts CHECK (max_attempts BETWEEN 1 AND 10),
        available_at timestamptz NOT NULL DEFAULT now(),
        lease_version bigint NOT NULL DEFAULT 0
            CONSTRAINT jobs_lease_version CHECK (lease_version >= 0),
        leased_by text CONSTRAINT jobs_leased_by CHECK (char_length(leased_by) <= 100),
        lease_expires_at timestamptz,
        heartbeat_at timestamptz,
        cancel_requested_at timestamptz,
        error_category text CONSTRAINT jobs_error_category CHECK (error_category IN ({CATEGORIES})),
        next_seq bigint NOT NULL DEFAULT 0 CONSTRAINT jobs_next_seq CHECK (next_seq >= 0),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        started_at timestamptz,
        finished_at timestamptz,
        CONSTRAINT jobs_attempt CHECK (attempt >= 0 AND attempt <= max_attempts),
        CONSTRAINT jobs_lease CHECK (
            (state = 'running') = (leased_by IS NOT NULL)
            AND (state = 'running') = (lease_expires_at IS NOT NULL)
            AND (state = 'running') = (heartbeat_at IS NOT NULL)),
        CONSTRAINT jobs_failed CHECK ((state = 'failed') = (error_category IS NOT NULL)),
        CONSTRAINT jobs_finished CHECK ((state IN ({TERMINAL})) = (finished_at IS NOT NULL)),
        CONSTRAINT jobs_owner_id_id_key UNIQUE (owner_id, id),
        CONSTRAINT jobs_owner_id_idempotency_key_key UNIQUE (owner_id, idempotency_key)
    )"""

JOB_INDEXES = (
    "CREATE INDEX jobs_queued ON public.jobs (available_at) WHERE state = 'queued'",
    "CREATE INDEX jobs_running ON public.jobs (lease_expires_at) WHERE state = 'running'",
    "CREATE INDEX jobs_owner_created ON public.jobs (owner_id, created_at)",
)

JOB_EVENTS = f"""
    CREATE TABLE public.job_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL,
        job_id uuid NOT NULL,
        seq bigint NOT NULL CONSTRAINT job_events_seq CHECK (seq >= 1),
        kind text NOT NULL CONSTRAINT job_events_kind CHECK (kind IN ({EVENT_KINDS})),
        payload jsonb NOT NULL DEFAULT '{{}}'::jsonb
            CONSTRAINT job_events_payload CHECK (octet_length(payload::text) <= 2048),
        lease_version bigint NOT NULL CONSTRAINT job_events_lease_version CHECK (lease_version >= 0),
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT job_events_owner_id_id_key UNIQUE (owner_id, id),
        CONSTRAINT job_events_owner_id_job_id_seq_key UNIQUE (owner_id, job_id, seq),
        CONSTRAINT job_events_job_fkey FOREIGN KEY (owner_id, job_id)
            REFERENCES public.jobs (owner_id, id) ON DELETE CASCADE
    )"""

EVENTS_APPEND_ONLY = (
    "CREATE POLICY events_no_update ON public.job_events AS RESTRICTIVE FOR UPDATE TO PUBLIC "
    "USING (false)",
    "CREATE POLICY events_no_delete ON public.job_events AS RESTRICTIVE FOR DELETE TO PUBLIC "
    "USING (false)",
)


def upgrade() -> None:
    op.execute(JOBS)
    enable_owner_rls("jobs")
    for statement in JOB_INDEXES:
        op.execute(statement)
    op.execute(JOB_EVENTS)
    enable_owner_rls("job_events")
    for statement in EVENTS_APPEND_ONLY:
        op.execute(statement)


def downgrade() -> None:
    # The queue functions stay (they are the superuser script's); they name these tables only at
    # run time, and the script grants ownvitals_queue its privileges again after the next upgrade.
    op.execute("DROP TABLE IF EXISTS public.job_events")
    op.execute("DROP TABLE IF EXISTS public.jobs")
