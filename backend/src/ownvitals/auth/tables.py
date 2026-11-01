"""The account system tables (ADR 0003 section 1), as SQLAlchemy Core definitions.

Only ``ownvitals.auth`` (and migrations and tests) may import this module or name these tables
in SQL; guard 8 (``tests/test_auth_tables_guard.py``) fails the build otherwise. The migration
``0005_auth`` creates the same tables; ``tests/test_auth_schema.py`` keeps the two in step.
"""

from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

_ID = "gen_random_uuid()"
_NOW = "now()"


def _id() -> Column[Any]:
    return Column("id", UUID(as_uuid=True), primary_key=True, server_default=text(_ID))


def _user_fk(*, nullable: bool = False, set_null: bool = False) -> Column[Any]:
    return Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL" if set_null else "CASCADE"),
        nullable=nullable or set_null,
    )


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> Column[Any]:
    return Column(
        name,
        DateTime(timezone=True),
        nullable=nullable,
        server_default=text(_NOW) if default_now else None,
    )


users = Table(
    "users",
    metadata,
    _id(),
    Column("email", Text, nullable=False),
    Column("email_normalized", Text, nullable=False, unique=True),
    _ts("email_verified_at", nullable=False),
    Column("password_hash", Text),
    Column("role", Text, nullable=False, server_default="user"),
    _ts("disabled_at"),
    _ts("created_at", nullable=False, default_now=True),
    CheckConstraint("role IN ('user', 'admin')", name="users_role_check"),
)

identities = Table(
    "identities",
    metadata,
    _id(),
    _user_fk(),
    Column("provider", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("email_at_link", Text),
    _ts("created_at", nullable=False, default_now=True),
    UniqueConstraint("provider", "subject", name="identities_provider_subject_key"),
    CheckConstraint("provider = 'google'", name="identities_provider"),
    Index("identities_user_id_idx", "user_id"),
)

invites = Table(
    "invites",
    metadata,
    _id(),
    Column("code_hash", LargeBinary, nullable=False, unique=True),
    Column("created_by", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    Column("used_by", UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    _ts("created_at", nullable=False, default_now=True),
    _ts("expires_at", nullable=False),
    _ts("used_at"),
)

sessions = Table(
    "sessions",
    metadata,
    _id(),
    _user_fk(),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    _ts("created_at", nullable=False, default_now=True),
    _ts("last_seen_at", nullable=False, default_now=True),
    _ts("expires_at", nullable=False),
    _ts("reauth_at"),
    _ts("mfa_at"),
    _ts("stepup_at"),
    Column("user_agent", Text),
    Column("ip", Text),
    Index("sessions_user_id_idx", "user_id"),
    Index("sessions_expires_at_idx", "expires_at"),
)

known_devices = Table(
    "known_devices",
    metadata,
    _id(),
    _user_fk(),
    Column("device_hash", LargeBinary, nullable=False, unique=True),
    _ts("created_at", nullable=False, default_now=True),
    _ts("last_used_at", nullable=False, default_now=True),
    Index("known_devices_user_id_idx", "user_id"),
)

pending_signups = Table(
    "pending_signups",
    metadata,
    _id(),
    Column("token_hash", LargeBinary, unique=True),
    Column("email", Text),
    Column("email_normalized", Text),  # not unique: each pending row has its own token
    Column("password_hash", Text),
    Column("provider", Text),
    Column("subject", Text),
    CheckConstraint("provider = 'google'", name="pending_signups_provider"),
    Column("browser_hash", LargeBinary),
    Column("invite_id", UUID(as_uuid=True), ForeignKey("invites.id", ondelete="CASCADE")),
    _ts("created_at", nullable=False, default_now=True),
    _ts("expires_at", nullable=False),
    CheckConstraint(
        "(provider IS NULL) = (subject IS NULL) AND (provider IS NULL) = (browser_hash IS NULL)",
        name="pending_signups_provider_together",
    ),
    CheckConstraint(
        "(email IS NULL) = (email_normalized IS NULL)", name="pending_signups_email_together"
    ),
    Index("pending_signups_email_normalized_idx", "email_normalized"),
)

auth_tokens = Table(
    "auth_tokens",
    metadata,
    _id(),
    Column("purpose", Text, nullable=False),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    _user_fk(),
    Column("new_email", Text),
    Column("email_at_issue", Text),
    _ts("created_at", nullable=False, default_now=True),
    _ts("expires_at", nullable=False),
    _ts("used_at"),
    CheckConstraint("purpose IN ('reset_password', 'change_email')", name="auth_tokens_purpose"),
    Index("auth_tokens_user_id_idx", "user_id"),
)

oauth_flows = Table(
    "oauth_flows",
    metadata,
    _id(),
    Column("flow_hash", LargeBinary, nullable=False, unique=True),
    Column("provider", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("state_hash", LargeBinary, nullable=False),
    Column("nonce", Text, nullable=False),
    Column("pkce_verifier", Text, nullable=False),
    CheckConstraint("pkce_verifier ~ '^[A-Za-z0-9_-]{64}$'", name="oauth_flows_pkce_verifier"),
    _user_fk(nullable=True),
    Column("invite_id", UUID(as_uuid=True), ForeignKey("invites.id", ondelete="CASCADE")),
    Column("return_to", Text),
    _ts("created_at", nullable=False, default_now=True),
    _ts("expires_at", nullable=False),
    CheckConstraint("provider = 'google'", name="oauth_flows_provider"),
    CheckConstraint("purpose IN ('sign_in', 'link', 'reauth')", name="oauth_flows_purpose"),
)

mfa_totp = Table(
    "mfa_totp",
    metadata,
    _id(),
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    # ADR 0004's envelope form only (ADR 0003 section 5).
    Column("secret_ciphertext", LargeBinary, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("key_id", Text, nullable=False),
    Column("wrapped_dek", LargeBinary, nullable=False),
    Column("dek_nonce", LargeBinary, nullable=False),
    _ts("confirmed_at"),
    Column("last_step", BigInteger, nullable=False, server_default="0"),
    CheckConstraint("octet_length(nonce) = 12", name="mfa_totp_nonce"),
    CheckConstraint("key_id ~ '^kek-[a-z0-9-]{1,28}$'", name="mfa_totp_key_id"),
    CheckConstraint("octet_length(wrapped_dek) = 48", name="mfa_totp_wrapped_dek"),
    CheckConstraint("octet_length(dek_nonce) = 12", name="mfa_totp_dek_nonce"),
)

mfa_recovery_codes = Table(
    "mfa_recovery_codes",
    metadata,
    _id(),
    _user_fk(),
    Column("code_hash", LargeBinary, nullable=False),
    _ts("used_at"),
    UniqueConstraint("user_id", "code_hash", name="mfa_recovery_codes_user_code_key"),
)

rate_limits = Table(
    "rate_limits",
    metadata,
    Column("bucket_hash", LargeBinary, primary_key=True),
    Column("window_start", DateTime(timezone=True), primary_key=True),
    Column("count", Integer, nullable=False),
    Index("rate_limits_window_start_idx", "window_start"),
)

auth_events = Table(
    "auth_events",
    metadata,
    _id(),
    _user_fk(set_null=True),
    Column("kind", Text, nullable=False),
    Column("ip", Text),
    _ts("at", nullable=False, default_now=True),
    Column("detail", JSONB),
    Index("auth_events_at_idx", "at"),
)

TABLE_NAMES = tuple(metadata.tables)
