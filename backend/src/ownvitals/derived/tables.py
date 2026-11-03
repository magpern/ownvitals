"""Derived tables (PLAN §5 and §7, roadmap Amendments 2 and 3, ADR 0010 §4, ADR 0002).

Migration ``0011_analysis_schema`` creates them; the check constraints live there. Every table
is owned: ``owner_id`` cascading from ``users``, forced RLS and the ``owner_isolation`` policy.
``observations`` is monthly partitioned on ``ts``: its key is ``(owner_id, id, ts)`` and nothing
may reference it (Amendment 2). The others keep ``UNIQUE (owner_id, id)``.

Provenance (PLAN §5): every derived row carries ``algo_version``, ``input_revision`` (the
``data_revisions`` counter it was computed from) and ``input_fingerprint`` (SHA-256, 32 bytes).
A row is stale when either input value differs.

The vocabularies below are the ones the migration's check constraints accept. They are copied
there, not imported, so a later edit here cannot change an applied migration; a schema test
holds both equal.
"""

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.sql.schema import SchemaItem

metadata = MetaData()

DERIVED_OWNED = ("observations", "metric_daily", "findings", "scores", "user_profiles")

# observations.quality_flags: ``ownvitals.analysis.quality.Flag``.
QUALITY_FLAGS = ("unconfirmed", "implausible", "duplicate", "jump")
# metric_daily.method: ``ownvitals.analysis.daily.DailyMethod``.
DAILY_METHODS = ("morning", "median")
# findings (PLAN §7.2; Amendment 3 adds ``system``, for rule Q1 only; ADR 0010 §4 to §6).
TIERS = ("device", "trend", "experimental", "system")
SYSTEM_RULE = "Q1"
SEVERITIES = ("urgent", "attention", "info")
CONFIDENCES = ("low", "medium", "high")
ORIGINS = ("live", "backfill")
STATUSES = ("active", "ended")
NOTIFY_STATES = ("none", "pending", "held", "sent")
# One active episode per (owner, rule, subject): ADR 0010 §4.
ACTIVE_EPISODE_INDEX = "findings_active_episode"
# user_profiles (the Settings mock-up, docs/design/mockups/src/Settings.dc.html).
SEXES = ("female", "male")
DAY_ASSIGNMENTS = ("reading_tz", "fixed_tz")
THEMES = ("system", "light", "dark")
WEIGHT_UNITS = ("kg", "lb", "st")
TEMPERATURE_UNITS = ("c", "f")
DATETIME_FORMATS = ("d_mon_y", "mon_d_y", "iso")
CHART_RANGES = ("30d", "90d", "6mo", "1y")
OVERVIEW_GROUPS = ("body", "heart", "nerve", "activity")
# scores.components.status (ADR 0012 section 1); only ``computed`` has a value.
SCORE_STATUSES = ("computed", "insufficient_data", "not_available", "not_supported", "not_set")
# metric_key: a name, then optionally ``:`` and a second part (ADR 0010's segmental keys).
METRIC_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,39}(:[a-z][a-z0-9_]{0,23})?$"
# Every tier notifies by default; the user may narrow it (``notify_tiers``).
DEFAULT_NOTIFY_TIERS = TIERS


def _owner() -> Column[Any]:
    return Column(
        "owner_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )


def _id(primary: bool = True) -> Column[Any]:
    return Column(
        "id",
        UUID(as_uuid=True),
        primary_key=primary,
        nullable=False,
        server_default=func.gen_random_uuid(),
    )


def _provenance() -> tuple[SchemaItem, ...]:
    return (
        Column("algo_version", Text, nullable=False),
        Column("input_revision", BigInteger, nullable=False),
        Column("input_fingerprint", LargeBinary, nullable=False),
    )


observations = Table(
    "observations",
    metadata,
    _id(primary=False),
    _owner(),
    Column("metric_key", Text, nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("source_ts", DateTime(timezone=True), nullable=False),
    Column("source_tz", Text, nullable=False),
    Column("value", Numeric, nullable=False),
    Column("source_value_raw", BigInteger, nullable=False),
    Column("source_unit", Integer, nullable=False),
    Column("conversion", Text, nullable=False),
    Column("quality_flags", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    # source_ref (PLAN §5): the measure it came from. Segmental types 173-175 repeat in one
    # group with different positions, so (grpid, type, position) is the key.
    Column("source_grpid", BigInteger),
    Column("source_type", Integer),
    Column("source_position", Integer),
    *_provenance(),
    PrimaryKeyConstraint("owner_id", "id", "ts"),
    UniqueConstraint(
        "owner_id",
        "metric_key",
        "source_grpid",
        "source_type",
        "source_position",
        "ts",
        name="observations_source_key",
        postgresql_nulls_not_distinct=True,
    ),
    Index("observations_ts", "ts", postgresql_using="brin"),
    postgresql_partition_by="RANGE (" + "ts)",
)

metric_daily = Table(
    "metric_daily",
    metadata,
    _id(),
    _owner(),
    Column("metric_key", Text, nullable=False),
    Column("day", Date, nullable=False),
    Column("method", Text, nullable=False),
    Column("value", Numeric, nullable=False),
    Column("morning", Numeric),
    Column("mean", Numeric, nullable=False),
    Column("min", Numeric, nullable=False),
    Column("max", Numeric, nullable=False),
    Column("mad", Numeric, nullable=False),
    Column("n", Integer, nullable=False),
    # The grpids the value came from (ADR 0006 §11's trigger rule): the morning reading's
    # group, or every group the median used.
    Column("source_groups", ARRAY(BigInteger), nullable=False),
    *_provenance(),
    UniqueConstraint("owner_id", "id"),
    UniqueConstraint("owner_id", "metric_key", "day", name="metric_daily_day_key"),
)

findings = Table(
    "findings",
    metadata,
    _id(),
    _owner(),
    Column("rule_id", Text, nullable=False),
    Column("rule_version", Text, nullable=False),
    Column("tier", Text, nullable=False),
    Column("severity", Text, nullable=False),
    Column("confidence", Text, nullable=False),
    Column("data_quality", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("origin", Text, nullable=False),
    Column("metric_keys", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("subject", Text, nullable=False),
    Column("window_start", Date, nullable=False),
    Column("window_end", Date, nullable=False),
    Column("reference_day", Date),
    Column("evidence", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("input_revision", BigInteger, nullable=False),
    Column("input_fingerprint", LargeBinary, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("first_seen", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("ended_at", DateTime(timezone=True)),
    Column("notify_state", Text, nullable=False, server_default=text("'none'")),
    Column("notified_at", DateTime(timezone=True)),
    Column("seen_at", DateTime(timezone=True)),
    UniqueConstraint("owner_id", "id"),
    Index(
        ACTIVE_EPISODE_INDEX,
        "owner_id",
        "rule_id",
        "subject",
        unique=True,
        postgresql_where=text("status = 'active'"),
    ),
)

scores = Table(
    "scores",
    metadata,
    _id(),
    _owner(),
    Column("kind", Text, nullable=False),
    Column("day", Date, nullable=False),
    # NULL unless components.status is computed (ADR 0012 sections 1 and 5).
    Column("value", Numeric),
    Column("components", JSONB, nullable=False),
    *_provenance(),
    UniqueConstraint("owner_id", "id"),
    UniqueConstraint("owner_id", "kind", "day", name="scores_day_key"),
)

user_profiles = Table(
    "user_profiles",
    metadata,
    _id(),
    _owner(),
    Column("birth_year", SmallInteger),
    Column("sex", Text),
    Column("day_assignment", Text, nullable=False, server_default=text("'reading_tz'")),
    Column("day_tz", Text),
    Column("theme", Text, nullable=False, server_default=text("'system'")),
    Column("weight_unit", Text, nullable=False, server_default=text("'kg'")),
    Column("temperature_unit", Text, nullable=False, server_default=text("'c'")),
    Column("datetime_format", Text, nullable=False, server_default=text("'d_mon_y'")),
    Column("chart_range", Text, nullable=False, server_default=text("'90d'")),
    Column(
        "overview_order",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{body,heart,nerve,activity}'::text[]"),
    ),
    Column("show_unsupported", Boolean, nullable=False, server_default=text("true")),
    Column("show_experimental", Boolean, nullable=False, server_default=text("false")),
    Column("notify_email", Boolean, nullable=False, server_default=text("false")),
    Column("notify_weekly", Boolean, nullable=False, server_default=text("false")),
    Column(
        "notify_tiers",
        ARRAY(Text),
        nullable=False,
        server_default=text("'{device,trend,experimental,system}'::text[]"),
    ),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("owner_id", "id"),
    UniqueConstraint("owner_id", name="user_profiles_owner_key"),
)
