"""provider connections (ADR 0004 section 1; #13)

Revision ID: 0007_connections
Revises: 0006_admin
Create Date: 2026-11-02

The owned ``connections`` table: one live row per user and provider, envelope credentials
that are all set or all empty, and the token generation the compare-and-swap write uses.

- ``UNIQUE (owner_id, id)`` and forced row-level security (ADR 0002 section 2).
- A unique index on ``(owner_id, provider) WHERE status <> 'disconnected'`` so a disconnected
  row does not block a new connection. Not a plain ``UNIQUE (owner_id, provider)``.
- The five credential columns are nullable together. ``connected`` requires them,
  ``disconnected`` forbids them, ``needs_reconnect`` allows either (a failed refresh keeps
  them; ``keys wipe`` removes them).
- ``withings`` is in the provider check for M1. ``test`` is the test-only provider: the
  database admits it, and only the test environment's routes do.
- Withings account columns (``app_id``, ``withings_userid``) are M1's. This migration moves
  no data and bypasses no row-level security.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0007_connections"
down_revision: str | Sequence[str] | None = "0006_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY_ID = "'^kek-[a-z0-9-]{1,28}$'"
_CATEGORIES = (
    "'needs_reconnect', 'rate_limited', 'upstream_unavailable', 'blocked_by_provider_plan', "
    "'key_unavailable', 'decrypt_failed', 'not_connected', 'revoke_failed', 'unknown'"
)

_CONNECTIONS = f"""
    CREATE TABLE public.connections (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
        provider text NOT NULL
            CONSTRAINT connections_provider CHECK (provider IN ('withings', 'test')),
        status text NOT NULL
            CONSTRAINT connections_status
            CHECK (status IN ('connected', 'needs_reconnect', 'disconnected')),
        last_error text CONSTRAINT connections_last_error CHECK (last_error IN ({_CATEGORIES})),
        public jsonb NOT NULL DEFAULT '{{}}'::jsonb
            CONSTRAINT connections_public CHECK (jsonb_typeof(public) = 'object'),
        secret bytea,
        secret_nonce bytea,
        wrapped_dek bytea,
        dek_nonce bytea,
        key_id text,
        token_generation bigint NOT NULL DEFAULT 0
            CONSTRAINT connections_token_generation CHECK (token_generation >= 0),
        refresh_in_flight bigint,
        refresh_intent_at timestamptz,
        expires_at timestamptz,
        connected_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        last_used_at timestamptz,
        CONSTRAINT connections_owner_id_id_key UNIQUE (owner_id, id),
        CONSTRAINT connections_credentials CHECK (
            (
                secret IS NULL AND secret_nonce IS NULL AND wrapped_dek IS NULL
                AND dek_nonce IS NULL AND key_id IS NULL
            )
            OR (
                secret IS NOT NULL AND secret_nonce IS NOT NULL AND wrapped_dek IS NOT NULL
                AND dek_nonce IS NOT NULL AND key_id IS NOT NULL
                AND octet_length(secret_nonce) = 12
                AND octet_length(dek_nonce) = 12
                AND octet_length(wrapped_dek) = 48
                AND key_id ~ {_KEY_ID}
            )
        ),
        CONSTRAINT connections_status_credentials CHECK (
            (status = 'connected' AND secret IS NOT NULL)
            OR (status = 'disconnected' AND secret IS NULL)
            OR status = 'needs_reconnect'
        )
    )"""

_ONE_LIVE = """
    CREATE UNIQUE INDEX connections_one_live
    ON public.connections (owner_id, provider)
    WHERE status <> 'disconnected'
    """


def upgrade() -> None:
    op.execute(_CONNECTIONS)
    op.execute(_ONE_LIVE)
    enable_owner_rls("connections")


def downgrade() -> None:
    op.execute("DROP TABLE public.connections")
