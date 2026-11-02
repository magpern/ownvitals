"""Ingest bookkeeping (PLAN §5): cursors, webhook notifications and revision counters.

``webhook_events`` is a system table. The app role has no privilege on it; a row is inserted
only through ``webhook_event_insert`` (``docker/postgres/withings/40-withings.sql``).
``sync_cursors`` and ``data_revisions`` are owned and follow forced RLS.
"""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

INGEST_OWNED = ("sync_cursors", "data_revisions")

sync_cursors = Table(
    "sync_cursors",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "owner_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    ),
    Column("connection_id", UUID(as_uuid=True), nullable=False),
    Column("stream", Text, nullable=False),
    Column("capability", Text),
    Column("lastupdate", BigInteger),
    Column("covered_from", DateTime(timezone=True)),
    Column("covered_to", DateTime(timezone=True)),
    Column("backfill_state", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("last_ok_at", DateTime(timezone=True)),
    Column("last_error_category", Text),
    Column("source_tz", Text),
    UniqueConstraint("owner_id", "id"),
    UniqueConstraint("owner_id", "connection_id", "stream"),
    ForeignKeyConstraint(
        ["owner_id", "connection_id"],
        ["connections.owner_id", "connections.id"],
        ondelete="CASCADE",
    ),
)

webhook_events = Table(
    "webhook_events",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column("app_id", Integer, ForeignKey("withings_apps.id", ondelete="RESTRICT"), nullable=False),
    Column("withings_userid", BigInteger, nullable=False),
    Column("appli", Integer, nullable=False),
    Column("startdate", BigInteger, nullable=False),
    Column("enddate", BigInteger, nullable=False),
    Column("dedupe_key", Text, nullable=False, unique=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("job_id", UUID(as_uuid=True)),
)

data_revisions = Table(
    "data_revisions",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
    Column(
        "owner_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    ),
    Column("stream", Text, nullable=False),
    Column("revision", BigInteger, nullable=False, server_default=text("0")),
    UniqueConstraint("owner_id", "id"),
    UniqueConstraint("owner_id", "stream"),
)
