"""accounts and sessions: the M1 system tables (ADR 0003 section 1)

Revision ID: 0002_auth
Revises: 0001_baseline
Create Date: 2026-10-28

Twelve system tables, no ``owner_id`` and no RLS (ADR 0002 section 3); only ``ownvitals.auth``
touches them. Every token is stored as a hash (bytea). The default privileges of
``ownvitals_owner`` (docker/postgres/init/10-roles.sql) give ``ownvitals_app`` SELECT, INSERT,
UPDATE and DELETE on each new table, and nothing else (no TRUNCATE, REFERENCES or TRIGGER), so
this migration needs no GRANT of its own. ``tests/test_auth_schema.py`` checks the privileges.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_auth"
down_revision: str | Sequence[str] | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PK = "id uuid PRIMARY KEY DEFAULT gen_random_uuid()"
_USER = "user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE"
_NOW = "timestamptz NOT NULL DEFAULT now()"

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
        provider text NOT NULL CONSTRAINT identities_provider CHECK (provider IN ('google', 'github', 'microsoft')),
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
        provider text,
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
        provider text NOT NULL CONSTRAINT oauth_flows_provider CHECK (provider IN ('google', 'github', 'microsoft')),
        purpose text NOT NULL CONSTRAINT oauth_flows_purpose CHECK (purpose IN ('sign_in', 'link', 'reauth')),
        state_hash bytea NOT NULL,
        nonce text,
        pkce_verifier text NOT NULL,
        user_id uuid REFERENCES users (id) ON DELETE CASCADE,
        invite_id uuid REFERENCES invites (id) ON DELETE CASCADE,
        return_to text,
        created_at {_NOW},
        expires_at timestamptz NOT NULL""",
    "mfa_totp": f"""
        {_PK},
        user_id uuid NOT NULL UNIQUE REFERENCES users (id) ON DELETE CASCADE,
        secret_ciphertext bytea NOT NULL,
        nonce bytea NOT NULL,
        key_id text NOT NULL,
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


def upgrade() -> None:
    for name, body in TABLES.items():
        op.execute(f"CREATE TABLE public.{name} ({body})")
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for name in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS public.{name} CASCADE")
