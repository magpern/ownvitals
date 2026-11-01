"""accounts and sessions: the account system tables (ADR 0003 section 1)

Revision ID: 0005_auth
Revises: 0004_connection_keks
Create Date: 2026-11-01

classbeat's ``0002_auth`` (at ``d806051``), Google only, with ADR 0003's changes:

- Twelve system tables, no ``owner_id`` and no RLS (ADR 0002 section 3); only ``ownvitals.auth``
  touches them (guard 8). Every token is stored as a hash (bytea). The default privileges of
  ``ownvitals_owner`` give ``ownvitals_app`` SELECT, INSERT, UPDATE and DELETE on each new table
  and nothing else, so this migration needs no GRANT of its own (``tests/test_auth_schema.py``).
- ``identities`` and ``oauth_flows`` allow provider ``google`` only (PLAN section 3).
- ``oauth_flows``: every flow has a nonce and a PKCE ``code_verifier`` of RFC 7636's form (the
  64 characters ``oauth_store`` draws), so a flow without a verifier cannot exist (#55's N15).
- ``mfa_totp`` holds only ADR 0004's envelope form from the first enrolment (ADR 0003 section 5):
  ``key_id`` is a KEK id, with a 48-byte wrapped data key and a 12-byte nonce. classbeat's
  interim ``app-v1`` form is not ported.
- ``jobs.owner_id`` and ``job_events.owner_id`` get their foreign keys to ``users`` (ADR 0002
  section 2, the #9 seam; ``docs/dev/isolation.md``, "Adding tables"): deleting an account
  removes its jobs and events (referential actions run without row security, so the events'
  append-only policies do not stop it).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_auth"
down_revision: str | Sequence[str] | None = "0004_connection_keks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PK = "id uuid PRIMARY KEY DEFAULT gen_random_uuid()"
_USER = "user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE"
_NOW = "timestamptz NOT NULL DEFAULT now()"
_KEK_ID = "'^kek-[a-z0-9-]{1,28}$'"

# Creation order = dependency order; the downgrade drops in reverse.
TABLES: dict[str, str] = {
    "users": f"""
        {_PK},
        email text NOT NULL,
        email_normalized text NOT NULL UNIQUE,
        email_verified_at timestamptz NOT NULL,
        password_hash text,
        role text NOT NULL DEFAULT 'user' CONSTRAINT users_role_check CHECK (role IN ('user', 'admin')),
        disabled_at timestamptz,
        created_at {_NOW}""",
    "identities": f"""
        {_PK},
        {_USER},
        provider text NOT NULL CONSTRAINT identities_provider CHECK (provider = 'google'),
        subject text NOT NULL,
        email_at_link text,
        created_at {_NOW},
        CONSTRAINT identities_provider_subject_key UNIQUE (provider, subject)""",
    "invites": f"""
        {_PK},
        code_hash bytea NOT NULL UNIQUE,
        created_by uuid REFERENCES users (id) ON DELETE SET NULL,
        used_by uuid REFERENCES users (id) ON DELETE SET NULL,
        created_at {_NOW},
        expires_at timestamptz NOT NULL,
        used_at timestamptz""",
    "sessions": f"""
        {_PK},
        {_USER},
        token_hash bytea NOT NULL UNIQUE,
        created_at {_NOW},
        last_seen_at {_NOW},
        expires_at timestamptz NOT NULL,
        reauth_at timestamptz,
        mfa_at timestamptz,
        stepup_at timestamptz,
        user_agent text,
        ip text""",
    "known_devices": f"""
        {_PK},
        {_USER},
        device_hash bytea NOT NULL UNIQUE,
        created_at {_NOW},
        last_used_at {_NOW}""",
    "pending_signups": f"""
        {_PK},
        token_hash bytea UNIQUE,
        email text,
        email_normalized text,
        password_hash text,
        provider text CONSTRAINT pending_signups_provider CHECK (provider = 'google'),
        subject text,
        browser_hash bytea,
        invite_id uuid REFERENCES invites (id) ON DELETE CASCADE,
        created_at {_NOW},
        expires_at timestamptz NOT NULL,
        CONSTRAINT pending_signups_provider_together CHECK (
            (provider IS NULL) = (subject IS NULL) AND (provider IS NULL) = (browser_hash IS NULL)),
        CONSTRAINT pending_signups_email_together CHECK ((email IS NULL) = (email_normalized IS NULL))""",
    "auth_tokens": f"""
        {_PK},
        purpose text NOT NULL CONSTRAINT auth_tokens_purpose CHECK (purpose IN ('reset_password', 'change_email')),
        token_hash bytea NOT NULL UNIQUE,
        {_USER},
        new_email text,
        email_at_issue text,
        created_at {_NOW},
        expires_at timestamptz NOT NULL,
        used_at timestamptz""",
    "oauth_flows": f"""
        {_PK},
        flow_hash bytea NOT NULL UNIQUE,
        provider text NOT NULL CONSTRAINT oauth_flows_provider CHECK (provider = 'google'),
        purpose text NOT NULL CONSTRAINT oauth_flows_purpose CHECK (purpose IN ('sign_in', 'link', 'reauth')),
        state_hash bytea NOT NULL,
        nonce text NOT NULL,
        pkce_verifier text NOT NULL
            CONSTRAINT oauth_flows_pkce_verifier CHECK (pkce_verifier ~ '^[A-Za-z0-9_-]{{64}}$'),
        user_id uuid REFERENCES users (id) ON DELETE CASCADE,
        invite_id uuid REFERENCES invites (id) ON DELETE CASCADE,
        return_to text,
        created_at {_NOW},
        expires_at timestamptz NOT NULL""",
    "mfa_totp": f"""
        {_PK},
        user_id uuid NOT NULL UNIQUE REFERENCES users (id) ON DELETE CASCADE,
        secret_ciphertext bytea NOT NULL,
        nonce bytea NOT NULL CONSTRAINT mfa_totp_nonce CHECK (octet_length(nonce) = 12),
        key_id text NOT NULL CONSTRAINT mfa_totp_key_id CHECK (key_id ~ {_KEK_ID}),
        wrapped_dek bytea NOT NULL CONSTRAINT mfa_totp_wrapped_dek CHECK (octet_length(wrapped_dek) = 48),
        dek_nonce bytea NOT NULL CONSTRAINT mfa_totp_dek_nonce CHECK (octet_length(dek_nonce) = 12),
        confirmed_at timestamptz,
        last_step bigint NOT NULL DEFAULT 0""",
    "mfa_recovery_codes": f"""
        {_PK},
        {_USER},
        code_hash bytea NOT NULL,
        used_at timestamptz,
        CONSTRAINT mfa_recovery_codes_user_code_key UNIQUE (user_id, code_hash)""",
    "rate_limits": """
        bucket_hash bytea NOT NULL,
        window_start timestamptz NOT NULL,
        count integer NOT NULL,
        PRIMARY KEY (bucket_hash, window_start)""",
    "auth_events": f"""
        {_PK},
        user_id uuid REFERENCES users (id) ON DELETE SET NULL,
        kind text NOT NULL,
        ip text,
        at {_NOW},
        detail jsonb""",
}

INDEXES = [
    "CREATE INDEX identities_user_id_idx ON identities (user_id)",
    "CREATE INDEX sessions_user_id_idx ON sessions (user_id)",
    "CREATE INDEX sessions_expires_at_idx ON sessions (expires_at)",
    "CREATE INDEX known_devices_user_id_idx ON known_devices (user_id)",
    "CREATE INDEX pending_signups_email_normalized_idx ON pending_signups (email_normalized)",
    "CREATE INDEX auth_tokens_user_id_idx ON auth_tokens (user_id)",
    "CREATE INDEX rate_limits_window_start_idx ON rate_limits (window_start)",
    "CREATE INDEX auth_events_at_idx ON auth_events (at)",
]

OWNER_FKS = {
    table: f"ALTER TABLE public.{table} ADD CONSTRAINT {table}_owner_id_fkey "
    "FOREIGN KEY (owner_id) REFERENCES public.users (id) ON DELETE CASCADE"
    for table in ("jobs", "job_events")
}


def upgrade() -> None:
    for name, body in TABLES.items():
        op.execute(f"CREATE TABLE public.{name} ({body})")
    for statement in INDEXES:
        op.execute(statement)
    for statement in OWNER_FKS.values():
        op.execute(statement)


def downgrade() -> None:
    for table in OWNER_FKS:
        op.execute(f"ALTER TABLE public.{table} DROP CONSTRAINT IF EXISTS {table}_owner_id_fkey")
    for name in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS public.{name} CASCADE")
