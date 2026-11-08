"""The passkey and QR approve-login tables (ADR 0011 sections 1, 2, 4 and 5; migrations
``0014_passkeys`` and ``0015_qr_login``). Moved out of ``tables`` unchanged (#249); ``tables``
re-exports them, and only ``ownvitals.auth`` imports either module (guard 8).
"""

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    LargeBinary,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID

from ownvitals.auth.table_base import id_column as _id
from ownvitals.auth.table_base import metadata
from ownvitals.auth.table_base import ts as _ts
from ownvitals.auth.table_base import user_fk as _user_fk

# Passkeys (ADR 0011 sections 1, 2 and 4; migration 0014_passkeys).
pre_sessions = Table(
    "pre_sessions",
    metadata,
    _id(),
    Column("token_hash", LargeBinary, nullable=False, unique=True),
    _ts("created_at", nullable=False, default_now=True),
    _ts("expires_at", nullable=False),
    Index("pre_sessions_expires_at_idx", "expires_at"),
)

webauthn_credentials = Table(
    "webauthn_credentials",
    metadata,
    _id(),
    _user_fk(),
    Column("credential_id", LargeBinary, nullable=False, unique=True),
    Column("public_key", LargeBinary, nullable=False),
    Column("sign_count", BigInteger, nullable=False, server_default="0"),
    Column("transports", ARRAY(Text), nullable=False, server_default="{}"),
    Column("aaguid", UUID(as_uuid=True)),
    Column("backup_eligible", Boolean, nullable=False, server_default="false"),
    Column("backup_state", Boolean, nullable=False, server_default="false"),
    Column("name", Text, nullable=False),
    _ts("created_at", nullable=False, default_now=True),
    _ts("last_used_at"),
    _ts("suspended_at"),
    Index("webauthn_credentials_user_id_idx", "user_id"),
)

webauthn_challenges = Table(
    "webauthn_challenges",
    metadata,
    _id(),
    Column("challenge_hash", LargeBinary, nullable=False, unique=True),
    Column("purpose", Text, nullable=False),
    _user_fk(nullable=True),
    Column("session_id", UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE")),
    Column("presession_id", UUID(as_uuid=True), ForeignKey("pre_sessions.id", ondelete="CASCADE")),
    _ts("created_at", nullable=False, default_now=True),
    _ts("expires_at", nullable=False),
    CheckConstraint(
        "purpose IN ('register', 'signin', 'reauth')", name="webauthn_challenges_purpose"
    ),
)

# QR approve-login (ADR 0011 section 5; migration 0015_qr_login).
qr_logins = Table(
    "qr_logins",
    metadata,
    _id(),
    Column("nonce_hash", LargeBinary, nullable=False, unique=True),
    Column("presession_id", UUID(as_uuid=True), ForeignKey("pre_sessions.id", ondelete="SET NULL")),
    Column("state", Text, nullable=False, server_default="pending"),
    _ts("created_at", nullable=False, default_now=True),
    _ts("scan_deadline", nullable=False),
    Column("desktop_ip", Text),
    Column("desktop_browser", Text, nullable=False),
    Column("desktop_os", Text, nullable=False),
    Column("desktop_place", Text),
    Column("approver_user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")),
    Column(
        "approver_session_id", UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL")
    ),
    _ts("scanned_at"),
    _ts("approve_deadline"),
    _ts("approved_at"),
    Column("approved_factor", Text),
    _ts("complete_deadline"),
    CheckConstraint(
        "state IN ('pending', 'scanned', 'approved', 'completed', 'denied', 'expired')",
        name="qr_logins_state",
    ),
)
