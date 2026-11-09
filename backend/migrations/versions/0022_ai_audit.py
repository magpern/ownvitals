"""The AI audit log with redacted content (ADR 0014 section 6; #207)

Revision ID: 0022_ai_audit
Revises: 0021_ai_consent
Create Date: 2026-11-09

``ai_audit`` (owned, forced RLS): one row per model call the worker made: the job, the kind
(``chat``, ``digest``, ``eval``), the model, the prompt and tool-set versions, the **names** of the
tools the answer asked for with their closed arguments, the token counts, the cost in micro-USD,
the reservation, the latency, the attempts, the status, a closed error category, the HTTP status,
how the reservation was settled, and an HMAC-SHA-256 of the request body and of the answer body
under a key derived from ``app_secret_key`` (HKDF label ``ai-audit``). **Never** message text,
tool results, the system prompt or the API key: no column can hold free text (every text column
has a closed vocabulary or a pattern; ``tools`` is a bounded JSON array whose content the writer
builds from closed sets only).

Append-only for the app role: ``SELECT`` and ``INSERT``, no ``UPDATE`` or ``DELETE``
(``10-roles.sql`` restores the same on a re-run). It survives consent withdrawal and "delete
imported data" (``KEPT``); account deletion removes it through the ``owner_id`` cascade. The
12-month retention purge is not part of this revision.

Expand only: one new table, no change to an existing one.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0022_ai_audit"
down_revision: str | Sequence[str] | None = "0021_ai_consent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT = """
CREATE TABLE public.ai_audit (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    job_id uuid,
    kind text NOT NULL CONSTRAINT ai_audit_kind CHECK (kind IN ('chat', 'digest', 'eval')),
    model text NOT NULL CONSTRAINT ai_audit_model CHECK (model ~ '^[a-z0-9][a-z0-9.\\-]{0,99}$'),
    prompt_version text NOT NULL
        CONSTRAINT ai_audit_prompt_version CHECK (prompt_version ~ '^[0-9]{1,6}$'),
    toolset_version text NOT NULL
        CONSTRAINT ai_audit_toolset_version CHECK (toolset_version ~ '^[0-9]{1,6}$'),
    tools jsonb NOT NULL DEFAULT '[]'::jsonb CONSTRAINT ai_audit_tools CHECK (
        jsonb_typeof(tools) = 'array' AND jsonb_array_length(tools) <= 16
        AND octet_length(tools::text) <= 4096
    ),
    input_token_count bigint NOT NULL DEFAULT 0
        CONSTRAINT ai_audit_input CHECK (input_token_count >= 0),
    output_token_count bigint NOT NULL DEFAULT 0
        CONSTRAINT ai_audit_output CHECK (output_token_count >= 0),
    cache_write_token_count bigint NOT NULL DEFAULT 0
        CONSTRAINT ai_audit_cache_write CHECK (cache_write_token_count >= 0),
    cache_read_token_count bigint NOT NULL DEFAULT 0
        CONSTRAINT ai_audit_cache_read CHECK (cache_read_token_count >= 0),
    cost_microusd bigint NOT NULL CONSTRAINT ai_audit_cost CHECK (cost_microusd >= 0),
    reservation_id uuid NOT NULL,
    latency_ms integer NOT NULL CONSTRAINT ai_audit_latency CHECK (latency_ms >= 0),
    attempts smallint NOT NULL CONSTRAINT ai_audit_attempts CHECK (attempts BETWEEN 1 AND 10),
    status text NOT NULL CONSTRAINT ai_audit_status CHECK (status IN ('completed', 'failed')),
    error text CONSTRAINT ai_audit_error CHECK (error IN (
        'refused', 'rate_limited', 'overloaded', 'server_error', 'http_other', 'timeout',
        'not_sent', 'transport', 'bad_response', 'consent', 'cancelled'
    )),
    http_status smallint CONSTRAINT ai_audit_http_status CHECK (http_status BETWEEN 100 AND 599),
    settlement text NOT NULL CONSTRAINT ai_audit_settlement CHECK (settlement IN (
        'settled', 'overrun', 'released', 'charged', 'unchanged', 'lost'
    )),
    request_hmac bytea NOT NULL
        CONSTRAINT ai_audit_request_hmac CHECK (octet_length(request_hmac) = 32),
    response_hmac bytea
        CONSTRAINT ai_audit_response_hmac CHECK (octet_length(response_hmac) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ai_audit_completed CHECK ((status = 'completed') = (error IS NULL)),
    CONSTRAINT ai_audit_owner_id_id_key UNIQUE (owner_id, id)
)"""
_ORDER = "CREATE INDEX ai_audit_owner_created ON public.ai_audit (owner_id, created_at)"
_GRANTS = (
    "REVOKE ALL ON TABLE public.ai_audit FROM ownvitals_app, PUBLIC",
    "GRANT SELECT, INSERT ON TABLE public.ai_audit TO ownvitals_app",
)


def upgrade() -> None:
    op.execute(_AUDIT)
    op.execute(_ORDER)
    enable_owner_rls("ai_audit")
    for statement in _GRANTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.ai_audit")
