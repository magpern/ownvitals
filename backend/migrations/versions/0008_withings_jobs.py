"""Withings job extensions (ADR 0005 section 8, #18)

Revision ID: 0008_withings_jobs
Revises: 0007_connections
Create Date: 2026-11-02

Origins, lanes, singleton keys and a webhook-secret target on ``jobs``, and ``dead_letter`` as a
terminal state. The queue functions stay in ``docker/postgres/queue/20-queue.sql``; this migration
only changes columns they read, so that script's version comment moves to v2 in the same change.

Idempotency keys stay unique per owner and are only present on ``user`` jobs (a partial unique
index). This migration moves no data and bypasses no row-level security.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_withings_jobs"
down_revision: str | Sequence[str] | None = "0007_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The event kinds the table allows after this migration (0003's list plus dead_letter).
EVENT_KINDS = (
    "'queued', 'claimed', 'progress', 'retry_scheduled', 'cancel_requested', 'cancelled', "
    "'completed', 'failed', 'requeued', 'dead_letter'"
)
_STATES = "'queued', 'running', 'completed', 'failed', 'dead_letter', 'cancelled'"
_TERMINAL = "'completed', 'failed', 'dead_letter', 'cancelled'"
_OLD_STATES = "'queued', 'running', 'completed', 'failed', 'cancelled'"
_OLD_TERMINAL = "'completed', 'failed', 'cancelled'"
_OLD_EVENTS = (
    "'queued', 'claimed', 'progress', 'retry_scheduled', 'cancel_requested', 'cancelled', "
    "'completed', 'failed', 'requeued'"
)

_ADD = """
    ALTER TABLE public.jobs
        ADD COLUMN origin text NOT NULL DEFAULT 'user',
        ADD COLUMN lane smallint NOT NULL DEFAULT 3,
        ADD COLUMN singleton_key text,
        ADD COLUMN target_secret_id uuid
"""
_CHECKS = """
    ALTER TABLE public.jobs
        ADD CONSTRAINT jobs_origin CHECK (
            origin IN ('user', 'webhook', 'scheduler', 'operator')),
        ADD CONSTRAINT jobs_lane CHECK (lane BETWEEN 1 AND 5),
        ADD CONSTRAINT jobs_singleton_key CHECK (
            singleton_key IS NULL OR singleton_key ~ '^[a-z0-9_:-]{1,80}$')
"""
_STATE = f"""
    ALTER TABLE public.jobs DROP CONSTRAINT jobs_state,
        ADD CONSTRAINT jobs_state CHECK (state IN ({_STATES})),
        DROP CONSTRAINT jobs_failed,
        ADD CONSTRAINT jobs_failed CHECK (
            (state IN ('failed', 'dead_letter')) = (error_category IS NOT NULL)),
        DROP CONSTRAINT jobs_finished,
        ADD CONSTRAINT jobs_finished CHECK (
            (state IN ({_TERMINAL})) = (finished_at IS NOT NULL))
"""
_EVENTS = f"""
    ALTER TABLE public.job_events DROP CONSTRAINT job_events_kind,
        ADD CONSTRAINT job_events_kind CHECK (kind IN ({EVENT_KINDS}))
"""
_KEYS = """
    ALTER TABLE public.jobs DROP CONSTRAINT jobs_owner_id_idempotency_key_key,
        ALTER COLUMN idempotency_key DROP NOT NULL,
        ALTER COLUMN request_hash DROP NOT NULL,
        ADD CONSTRAINT jobs_user_idempotency CHECK (
            (origin = 'user') = (
                idempotency_key IS NOT NULL AND request_hash IS NOT NULL))
"""
_INDEXES = (
    "CREATE UNIQUE INDEX jobs_owner_id_idempotency_key_key "
    "ON public.jobs (owner_id, idempotency_key) WHERE idempotency_key IS NOT NULL",
    "CREATE UNIQUE INDEX jobs_singleton_queued ON public.jobs (owner_id, singleton_key) "
    "WHERE state = 'queued' AND singleton_key IS NOT NULL",
    "CREATE INDEX jobs_cancel_secret ON public.jobs (kind, target_secret_id) "
    "WHERE state = 'queued' AND target_secret_id IS NOT NULL",
)

_RESTORE_STATE = f"""
    ALTER TABLE public.jobs DROP CONSTRAINT jobs_state,
        ADD CONSTRAINT jobs_state CHECK (state IN ({_OLD_STATES})),
        DROP CONSTRAINT jobs_failed,
        ADD CONSTRAINT jobs_failed CHECK ((state = 'failed') = (error_category IS NOT NULL)),
        DROP CONSTRAINT jobs_finished,
        ADD CONSTRAINT jobs_finished CHECK (
            (state IN ({_OLD_TERMINAL})) = (finished_at IS NOT NULL))
"""
_RESTORE_EVENTS = f"""
    ALTER TABLE public.job_events DROP CONSTRAINT job_events_kind,
        ADD CONSTRAINT job_events_kind CHECK (kind IN ({_OLD_EVENTS}))
"""
_RESTORE_KEYS = """
    ALTER TABLE public.jobs DROP CONSTRAINT jobs_user_idempotency,
        ALTER COLUMN idempotency_key SET NOT NULL,
        ALTER COLUMN request_hash SET NOT NULL,
        ADD CONSTRAINT jobs_owner_id_idempotency_key_key UNIQUE (owner_id, idempotency_key)
"""


def upgrade() -> None:
    op.execute(_ADD)
    op.execute(_CHECKS)
    op.execute(_STATE)
    op.execute(_EVENTS)
    op.execute(_KEYS)
    for statement in _INDEXES:
        op.execute(statement)


def downgrade() -> None:
    # Forced RLS would hide the rows from this role, so the delete would not see them.
    # Rows only this revision can store cannot go back to the old checks.
    op.execute("ALTER TABLE public.jobs DISABLE ROW LEVEL SECURITY")
    op.execute(
        "DELETE FROM public.jobs WHERE origin <> 'user' OR state = 'dead_letter' "
        "OR idempotency_key IS NULL OR id IN ("
        "SELECT job_id FROM public.job_events WHERE kind = 'dead_letter')"
    )
    op.execute("ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.jobs FORCE ROW LEVEL SECURITY")
    op.execute("DROP INDEX public.jobs_cancel_secret")
    op.execute("DROP INDEX public.jobs_singleton_queued")
    op.execute("DROP INDEX public.jobs_owner_id_idempotency_key_key")
    op.execute(_RESTORE_KEYS)
    op.execute(_RESTORE_EVENTS)
    op.execute(_RESTORE_STATE)
    op.execute("ALTER TABLE public.jobs DROP CONSTRAINT jobs_singleton_key")
    op.execute("ALTER TABLE public.jobs DROP CONSTRAINT jobs_lane")
    op.execute("ALTER TABLE public.jobs DROP CONSTRAINT jobs_origin")
    op.execute(
        "ALTER TABLE public.jobs DROP COLUMN target_secret_id, "
        "DROP COLUMN singleton_key, DROP COLUMN lane, DROP COLUMN origin"
    )
