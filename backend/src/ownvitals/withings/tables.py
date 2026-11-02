"""Raw Withings tables (PLAN §5, ADR 0002, roadmap Amendment 2).

Migration ``0009_withings_schema`` creates them. ``withings_apps`` is a system table: no
``owner_id``, no row-level security. The owned tables each have ``owner_id`` and forced RLS.
``sleep_series`` and ``intraday`` are monthly partitions; their key is
``(owner_id, id, ts)`` and nothing references them.

Column lists here are the grants. The migration, ``10-roles.sql`` and the withings
``db-functions`` script must name the same columns (a schema test checks).
"""

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql.schema import SchemaItem

metadata = MetaData()

# What ``ownvitals_app`` may read. No write. The client secret is the enveloped ciphertext
# (bytea) plus its nonce, wrapped DEK and key id, the same shape as a connection.
APP_WITHINGS_COLUMNS = (
    "id",
    "client_id",
    "callback_base",
    "client_secret",
    "client_secret_nonce",
    "client_wrapped_dek",
    "client_dek_nonce",
    "client_key_id",
    "webhook_secrets",
    "rotation_requested_at",
    "rotation_reason",
)
# What ``ownvitals_maint`` may read. Not the client secret, its envelope, or webhook_secrets.
MAINT_WITHINGS_COLUMNS = (
    "id",
    "client_id",
    "callback_base",
    "rotation_requested_at",
    "rotation_reason",
)

withings_apps = Table(
    "withings_apps",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("client_id", Text, nullable=False),
    Column("callback_base", Text, nullable=False),
    Column("client_secret", LargeBinary, nullable=False),
    Column("client_secret_nonce", LargeBinary, nullable=False),
    Column("client_wrapped_dek", LargeBinary, nullable=False),
    Column("client_dek_nonce", LargeBinary, nullable=False),
    Column("client_key_id", Text, nullable=False),
    Column("webhook_secrets", JSONB, nullable=False),
    Column("rotation_requested_at", DateTime(timezone=True)),
    Column("rotation_reason", Text),
)


def _connection_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["owner_id", "connection_id"],
        ["connections.owner_id", "connections.id"],
        ondelete="CASCADE",
    )


def _owned(*columns: SchemaItem) -> tuple[SchemaItem, ...]:
    return (
        Column("id", UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()),
        Column(
            "owner_id",
            UUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *columns,
        UniqueConstraint("owner_id", "id"),
    )


devices = Table(
    "devices",
    metadata,
    *_owned(
        Column("connection_id", UUID(as_uuid=True), nullable=False),
        Column("hash_deviceid", Text, nullable=False),
        Column("model_id", Integer),
        Column("type", Text),
        Column("fw", Text),
        Column("battery", Text),
        Column("last_session_at", DateTime(timezone=True)),
        Column("source_tz", Text),
    ),
    _connection_fk(),
    UniqueConstraint("owner_id", "connection_id", "hash_deviceid"),
)

measure_groups = Table(
    "measure_groups",
    metadata,
    *_owned(
        Column("connection_id", UUID(as_uuid=True), nullable=False),
        Column("grpid", BigInteger, nullable=False),
        Column("measured_at", DateTime(timezone=True), nullable=False),
        Column("source_tz", Text, nullable=False),
        Column("attrib", Integer),
        Column("category", Integer),
        Column("model_id", Integer),
        Column("hash_deviceid", Text),
        Column("created_at", DateTime(timezone=True)),
        Column("modified_at", DateTime(timezone=True)),
        Column("deleted_at", DateTime(timezone=True)),
        Column("raw", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("payload_hash", LargeBinary, nullable=False),
    ),
    _connection_fk(),
    UniqueConstraint("owner_id", "connection_id", "grpid"),
)

measures = Table(
    "measures",
    metadata,
    *_owned(
        Column("measure_group_id", UUID(as_uuid=True), nullable=False),
        Column("type", Integer, nullable=False),
        Column("value_raw", BigInteger, nullable=False),
        Column("unit", Integer, nullable=False),
        Column("position", Integer),
    ),
    ForeignKeyConstraint(
        ["owner_id", "measure_group_id"],
        ["measure_groups.owner_id", "measure_groups.id"],
        ondelete="CASCADE",
    ),
)

ecg_recordings = Table(
    "ecg_recordings",
    metadata,
    *_owned(
        Column("connection_id", UUID(as_uuid=True), nullable=False),
        Column("signalid", BigInteger, nullable=False),
        Column("recorded_at", DateTime(timezone=True), nullable=False),
        Column("source_tz", Text, nullable=False),
        Column("model_id", Integer),
        Column("afib", Integer),
        Column("heart_rate", Integer),
        Column("sampling_hz", Integer),
        Column("wearposition", Integer),
        Column("signal", LargeBinary),
        Column("payload_hash", LargeBinary),
        Column("reported", JSONB),
        Column("estimated", JSONB),
        Column("afib_details", JSONB),
    ),
    _connection_fk(),
    UniqueConstraint("owner_id", "connection_id", "signalid"),
)

sleep_summaries = Table(
    "sleep_summaries",
    metadata,
    *_owned(
        Column("connection_id", UUID(as_uuid=True), nullable=False),
        Column("day", Date, nullable=False),
        Column("start_at", DateTime(timezone=True), nullable=False),
        Column("end_at", DateTime(timezone=True), nullable=False),
        Column("source_tz", Text, nullable=False),
        Column("model_id", Integer),
        Column("raw", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("payload_hash", LargeBinary, nullable=False),
    ),
    _connection_fk(),
    UniqueConstraint("owner_id", "connection_id", "day"),
)

activity_daily = Table(
    "activity_daily",
    metadata,
    *_owned(
        Column("connection_id", UUID(as_uuid=True), nullable=False),
        Column("day", Date, nullable=False),
        Column("start_at", DateTime(timezone=True), nullable=False),
        Column("source_tz", Text, nullable=False),
        Column("raw", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("payload_hash", LargeBinary, nullable=False),
    ),
    _connection_fk(),
    UniqueConstraint("owner_id", "connection_id", "day"),
)

workouts = Table(
    "workouts",
    metadata,
    *_owned(
        Column("connection_id", UUID(as_uuid=True), nullable=False),
        Column("workout_id", BigInteger, nullable=False),
        Column("start_at", DateTime(timezone=True), nullable=False),
        Column("end_at", DateTime(timezone=True), nullable=False),
        Column("source_tz", Text, nullable=False),
        Column("category", Integer),
        Column("raw", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
        Column("payload_hash", LargeBinary, nullable=False),
    ),
    _connection_fk(),
    UniqueConstraint("owner_id", "connection_id", "workout_id"),
)


def _partitioned(name: str, natural: tuple[str, ...], *columns: SchemaItem) -> Table:
    """A monthly leaf table. ``id`` alone is not unique; dedupe uses ``natural`` plus ``ts``."""
    return Table(
        name,
        metadata,
        Column("id", UUID(as_uuid=True), nullable=False, server_default=func.gen_random_uuid()),
        Column(
            "owner_id",
            UUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("ts", DateTime(timezone=True), nullable=False),
        Column("source_tz", Text, nullable=False),
        Column("connection_id", UUID(as_uuid=True), nullable=False),
        *columns,
        PrimaryKeyConstraint("owner_id", "id", "ts"),
        UniqueConstraint("owner_id", "connection_id", *natural, "ts"),
        _connection_fk(),
        postgresql_partition_by="RANGE (" + "ts)",
    )


sleep_series = _partitioned(
    "sleep_series",
    ("series",),
    Column("series", Text, nullable=False),
    Column("value_raw", BigInteger, nullable=False),
    Column("unit", Integer, nullable=False),
)
intraday = _partitioned(
    "intraday",
    ("metric",),
    Column("metric", Text, nullable=False),
    Column("value_raw", BigInteger, nullable=False),
    Column("unit", Integer, nullable=False),
)

# Owner-less unique index on the generated Withings account columns (ADR 0002 section 2).
CONNECTIONS_WITHINGS_USER_INDEX = "connections_app_withings_userid"

WITHINGS_OWNED = (
    "devices",
    "measure_groups",
    "measures",
    "ecg_recordings",
    "sleep_summaries",
    "activity_daily",
    "workouts",
    "sleep_series",
    "intraday",
)
