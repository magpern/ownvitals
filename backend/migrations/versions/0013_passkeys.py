"""passkeys: WebAuthn credentials, challenges and the pre-session (ADR 0011 sections 1, 2, 4)

Revision ID: 0013_passkeys
Revises: 0010_withings_rate
Create Date: 2026-11-03

Three new account system tables (ADR 0002 section 3, ADR 0011 section 2: sign-in finds a
credential before it knows the user, so they are system tables reached only through
``ownvitals.auth``, guard 8), and one column on ``users``:

- ``users.webauthn_handle``: the random 32-byte WebAuthn user handle, created at the first
  registration; never the user id or the email.
- ``pre_sessions``: the pre-session cookie's SHA-256, 5 minutes' life (section 4).
- ``webauthn_credentials``: one row per passkey. The public key is not a secret and is stored as
  returned (COSE). ``sign_count`` fits WebAuthn's unsigned 32-bit counter.
- ``webauthn_challenges``: the SHA-256 of each challenge, its purpose and what it is bound to:
  the user's session for ``register`` and ``reauth``, a pre-session for ``signin``. Each is
  deleted with what it is bound to.

The default privileges of ``ownvitals_owner`` give ``ownvitals_app`` SELECT, INSERT, UPDATE and
DELETE on each new table and nothing else, so no GRANT is needed (``tests/test_auth_schema.py``).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_passkeys"
down_revision: str | Sequence[str] | None = "0010_withings_rate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PK = "id uuid PRIMARY KEY DEFAULT gen_random_uuid()"
_NOW = "timestamptz NOT NULL DEFAULT now()"

TABLES: dict[str, str] = {
    "pre_sessions": f"""
        {_PK},
        token_hash bytea NOT NULL UNIQUE
            CONSTRAINT pre_sessions_token_hash CHECK (octet_length(token_hash) = 32),
        created_at {_NOW},
        expires_at timestamptz NOT NULL""",
    "webauthn_credentials": f"""
        {_PK},
        user_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        credential_id bytea NOT NULL UNIQUE CONSTRAINT webauthn_credentials_credential_id
            CHECK (octet_length(credential_id) BETWEEN 1 AND 1023),
        public_key bytea NOT NULL CONSTRAINT webauthn_credentials_public_key
            CHECK (octet_length(public_key) BETWEEN 1 AND 2048),
        sign_count bigint NOT NULL DEFAULT 0 CONSTRAINT webauthn_credentials_sign_count
            CHECK (sign_count BETWEEN 0 AND 4294967295),
        transports text[] NOT NULL DEFAULT '{{}}' CONSTRAINT webauthn_credentials_transports
            CHECK (cardinality(transports) <= 8),
        aaguid uuid,
        backup_eligible boolean NOT NULL DEFAULT false,
        backup_state boolean NOT NULL DEFAULT false,
        name text NOT NULL CONSTRAINT webauthn_credentials_name
            CHECK (char_length(name) BETWEEN 1 AND 64),
        created_at {_NOW},
        last_used_at timestamptz,
        suspended_at timestamptz""",
    "webauthn_challenges": f"""
        {_PK},
        challenge_hash bytea NOT NULL UNIQUE
            CONSTRAINT webauthn_challenges_challenge_hash CHECK (octet_length(challenge_hash) = 32),
        purpose text NOT NULL CONSTRAINT webauthn_challenges_purpose
            CHECK (purpose IN ('register', 'signin', 'reauth')),
        user_id uuid REFERENCES users (id) ON DELETE CASCADE,
        session_id uuid REFERENCES sessions (id) ON DELETE CASCADE,
        presession_id uuid REFERENCES pre_sessions (id) ON DELETE CASCADE,
        created_at {_NOW},
        expires_at timestamptz NOT NULL,
        CONSTRAINT webauthn_challenges_binding CHECK (
            (purpose = 'signin' AND presession_id IS NOT NULL AND session_id IS NULL
                AND user_id IS NULL)
            OR (purpose IN ('register', 'reauth') AND session_id IS NOT NULL
                AND user_id IS NOT NULL AND presession_id IS NULL))""",
}

INDEXES = [
    "CREATE INDEX pre_sessions_expires_at_idx ON pre_sessions (expires_at)",
    "CREATE INDEX webauthn_credentials_user_id_idx ON webauthn_credentials (user_id)",
    "CREATE INDEX webauthn_challenges_session_id_idx ON webauthn_challenges (session_id)",
    "CREATE INDEX webauthn_challenges_presession_id_idx ON webauthn_challenges (presession_id)",
    "CREATE INDEX webauthn_challenges_user_id_idx ON webauthn_challenges (user_id)",
    "CREATE INDEX webauthn_challenges_expires_at_idx ON webauthn_challenges (expires_at)",
]


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.users ADD COLUMN webauthn_handle bytea "
        "CONSTRAINT users_webauthn_handle_key UNIQUE "
        "CONSTRAINT users_webauthn_handle CHECK (octet_length(webauthn_handle) = 32)"
    )
    for name, body in TABLES.items():
        op.execute(f"CREATE TABLE public.{name} ({body})")
    for statement in INDEXES:
        op.execute(statement)


def downgrade() -> None:
    for name in reversed(TABLES):
        op.execute(f"DROP TABLE IF EXISTS public.{name} CASCADE")
    op.execute("ALTER TABLE public.users DROP COLUMN IF EXISTS webauthn_handle")
