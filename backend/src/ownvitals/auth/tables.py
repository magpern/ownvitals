"""The account system tables (ADR 0003 section 1), as SQLAlchemy Core definitions.

Only ``ownvitals.auth`` (and migrations and tests) may import this module or name these tables
in SQL; guard 8 (``tests/test_auth_tables_guard.py``) fails the build otherwise. The migration
``0005_auth`` creates the first twelve tables, ``0014_passkeys`` the passkey tables (ADR 0011
sections 2 and 4) and ``0015_qr_login`` the QR approve-login table (section 5);
``tests/test_auth_schema.py`` keeps the migrations and this module in step. The passkey and QR
tables are defined in ``tables_passkeys`` and re-exported here; the ``MetaData`` and the column
helpers are in ``table_base`` (#249).
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from ownvitals.auth.table_base import id_column as _id
from ownvitals.auth.table_base import metadata
from ownvitals.auth.table_base import ts as _ts
from ownvitals.auth.table_base import user_fk as _user_fk
from ownvitals.auth.tables_passkeys import (  # noqa: F401 - re-exported
    pre_sessions,
    qr_logins,
    webauthn_challenges,
    webauthn_credentials,
)

# The allowlist of the three ``locale`` columns: every code of ``frontend/src/i18n/locales.json``
# (tests/test_account_locale.py). The migration that adds a language widens it.
LOCALE_CHECK = "locale IN ('en', 'sv')"

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
    # The random WebAuthn user handle (ADR 0011 section 1), set at the first registration.
    Column("webauthn_handle", LargeBinary, unique=True),
    CheckConstraint("role IN ('user', 'admin')", name="users_role_check"),
    # The account's language (ADR 0015 section 2); NULL follows the browser. Migration 0017.
    Column("locale", Text),
    CheckConstraint(LOCALE_CHECK, name="users_locale"),
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
    # QR approve-login (ADR 0011 section 5; migration 0015_qr_login).
    Column("origin", Text, nullable=False, server_default="signin"),
    Column("mfa_by_approval", Boolean, nullable=False, server_default="false"),
    Column("approved_factor", Text),
    Column("approved_by_session_id", UUID(as_uuid=True)),
    # The session anomaly signal (ADR 0018; migration 0029_session_anomaly, #461).
    _ts("seen_window_at"),
    Column("seen_count", Integer, nullable=False, server_default="0"),
    Column("seen_country", Text),
    _ts("anomaly_at"),
    CheckConstraint("seen_count >= 0", name="sessions_seen_count"),
    CheckConstraint("seen_country ~ '^[A-Z0-9]{2}$'", name="sessions_seen_country"),
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
    Column("locale", Text),  # the language in effect at sign-up, for ``users.locale``
    CheckConstraint(LOCALE_CHECK, name="pending_signups_locale"),
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
    Column("locale", Text),  # a sign-in's language in effect at the start (no body at callback)
    CheckConstraint(LOCALE_CHECK, name="oauth_flows_locale"),
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
    # The request's coarse origin (``event_origin``, migration 0019_auth_event_origin, #251).
    Column("browser", Text),
    Column("os", Text),
    Column("place", Text),
    CheckConstraint("char_length(browser) BETWEEN 1 AND 32", name="auth_events_browser"),
    CheckConstraint("char_length(os) BETWEEN 1 AND 32", name="auth_events_os"),
    CheckConstraint("char_length(place) BETWEEN 1 AND 64", name="auth_events_place"),
    Index("auth_events_at_idx", "at"),
    Index("auth_events_user_at_idx", "user_id", "at", "id"),
    # The admin abuse view (migration 0029_session_anomaly, #461).
    Index(
        "auth_events_anomaly_at_idx", "at", "id", postgresql_where=text("kind = 'session_anomaly'")
    ),
)

TABLE_NAMES = tuple(metadata.tables)
