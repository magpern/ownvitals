"""The ``connections`` table (ADR 0004 section 1).

Only ``ownvitals.connections`` reads or writes it (``tests/crypto_scan.py``). Migration
``0007_connections`` creates it. The application supplies ``id`` before encrypting, because
the AAD names the row.
"""

import re

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

# Stored providers. ``withings`` is admitted for M1 and not connected yet. ``test`` is the
# test-only provider: routes offer it only when the environment is ``test``.
STORED_PROVIDERS = ("withings", "test")
TEST_PROVIDER = "test"
STATUSES = ("connected", "needs_reconnect", "disconnected")
DISCONNECTED = "disconnected"
PROVIDER_NAME = re.compile(r"[a-z][a-z0-9]{0,31}")

metadata = MetaData()
connections = Table(
    "connections",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("owner_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("last_error", Text),
    Column("public", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("secret", LargeBinary),
    Column("secret_nonce", LargeBinary),
    Column("wrapped_dek", LargeBinary),
    Column("dek_nonce", LargeBinary),
    Column("key_id", Text),
    Column("token_generation", BigInteger, nullable=False, server_default=text("0")),
    Column("refresh_in_flight", BigInteger),
    Column("refresh_intent_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("connected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_used_at", DateTime(timezone=True)),
    CheckConstraint(
        "(secret IS NULL AND secret_nonce IS NULL AND wrapped_dek IS NULL "
        "AND dek_nonce IS NULL AND key_id IS NULL) OR "
        "(secret IS NOT NULL AND secret_nonce IS NOT NULL AND wrapped_dek IS NOT NULL "
        "AND dek_nonce IS NOT NULL AND key_id IS NOT NULL)",
        name="connections_credentials",
    ),
    Index("connections_owner_id_id_key", "owner_id", "id", unique=True),
    Index(
        "connections_one_live",
        "owner_id",
        "provider",
        unique=True,
        postgresql_where=text("status <> 'disconnected'"),
    ),
)


def live_providers(environment: str) -> frozenset[str]:
    """Providers a connect route may store. Production and dev have none until M1."""
    if environment == "test":
        return frozenset({TEST_PROVIDER})
    return frozenset()
