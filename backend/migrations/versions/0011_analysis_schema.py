"""analysis schema: observations, metric_daily, findings, scores, user_profiles (#181)

Revision ID: 0011_analysis_schema
Revises: 0010_withings_rate
Create Date: 2026-11-03

PLAN section 5 and 7, roadmap Amendments 2 and 3, ADR 0010 section 4, ADR 0002 section 2.
Every table here is owned: ``owner_id`` cascading from ``users``, forced RLS with the
``owner_isolation`` policy, and ``UNIQUE (owner_id, id)``. ``observations`` is monthly
partitioned on ``ts`` and keyed ``PRIMARY KEY (owner_id, id, ts)`` instead; nothing references
it. Its partitions are created by ``ensure_month_partitions`` (40-withings.sql), never here.

Expand only: five new tables, no change to an existing one, no data moved, no row-level
security bypassed. The downgrade drops them (and the partitions with their parent).

The vocabularies are written out here on purpose, not imported, so an applied migration never
changes; ``ownvitals.derived.tables`` holds the same lists and a schema test compares them.
"""

from collections.abc import Sequence

from alembic import op

from ownvitals.db.rls import enable_owner_rls

revision: str = "0011_analysis_schema"
down_revision: str | Sequence[str] | None = "0010_withings_rate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNED = ("observations", "metric_daily", "findings", "scores", "user_profiles")

_KEY = "'^[a-z][a-z0-9_]{0,39}$'"
_TZ = "char_length({col}) BETWEEN 1 AND 64"
# Finite and bounded: numeric 'NaN' sorts above every number, so BETWEEN refuses it, and the
# infinities too.
_FINITE = "{col} BETWEEN -1e12 AND 1e12"


def _provenance(table: str) -> str:
    return f"""
    algo_version text NOT NULL CONSTRAINT {table}_algo_version
        CHECK (algo_version ~ '^[a-z][a-z0-9_-]{{0,31}}/[0-9]{{1,6}}$'),
    input_revision bigint NOT NULL CONSTRAINT {table}_input_revision CHECK (input_revision >= 0),
    input_fingerprint bytea NOT NULL CONSTRAINT {table}_input_fingerprint
        CHECK (octet_length(input_fingerprint) = 32),"""


_OBSERVATIONS = f"""
CREATE TABLE public.observations (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    metric_key text NOT NULL CONSTRAINT observations_metric_key CHECK (metric_key ~ {_KEY}),
    ts timestamptz NOT NULL,
    source_ts timestamptz NOT NULL,
    source_tz text NOT NULL CONSTRAINT observations_source_tz CHECK ({_TZ.format(col="source_tz")}),
    value numeric NOT NULL CONSTRAINT observations_value CHECK ({_FINITE.format(col="value")}),
    source_value_raw bigint NOT NULL,
    source_unit integer NOT NULL CONSTRAINT observations_source_unit
        CHECK (source_unit BETWEEN -30 AND 30),
    conversion text NOT NULL CONSTRAINT observations_conversion
        CHECK (conversion ~ '^[a-z0-9][a-z0-9_.:/-]{{0,63}}$'),
    quality_flags text[] NOT NULL DEFAULT '{{}}'::text[]
        CONSTRAINT observations_quality_flags CHECK (
            quality_flags <@ ARRAY['unconfirmed', 'implausible', 'duplicate', 'jump']::text[]
            AND array_position(quality_flags, NULL) IS NULL
        ),
    source_grpid bigint,
    source_type integer,
    source_position integer,
    {_provenance("observations")}
    PRIMARY KEY (owner_id, id, ts),
    CONSTRAINT observations_source_key UNIQUE NULLS NOT DISTINCT
        (owner_id, metric_key, source_grpid, source_type, source_position, ts),
    CONSTRAINT observations_source_ref CHECK (
        (source_type IS NULL AND source_position IS NULL) OR source_grpid IS NOT NULL
    )
) PARTITION BY RANGE (ts)"""

_DAILY = f"""
CREATE TABLE public.metric_daily (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    metric_key text NOT NULL CONSTRAINT metric_daily_metric_key CHECK (metric_key ~ {_KEY}),
    day date NOT NULL,
    method text NOT NULL CONSTRAINT metric_daily_method CHECK (method IN ('morning', 'median')),
    value numeric NOT NULL,
    morning numeric,
    mean numeric NOT NULL,
    min numeric NOT NULL,
    max numeric NOT NULL,
    mad numeric NOT NULL,
    n integer NOT NULL CONSTRAINT metric_daily_n CHECK (n >= 1),
    source_groups bigint[] NOT NULL CONSTRAINT metric_daily_source_groups CHECK (
        cardinality(source_groups) BETWEEN 1 AND n
        AND array_position(source_groups, NULL) IS NULL
    ),
    {_provenance("metric_daily")}
    CONSTRAINT metric_daily_owner_id_id_key UNIQUE (owner_id, id),
    CONSTRAINT metric_daily_day_key UNIQUE (owner_id, metric_key, day),
    CONSTRAINT metric_daily_finite CHECK (
        {_FINITE.format(col="value")} AND {_FINITE.format(col="mean")}
        AND {_FINITE.format(col="min")} AND {_FINITE.format(col="max")}
        AND mad BETWEEN 0 AND 1e12
        AND (morning IS NULL OR {_FINITE.format(col="morning")})
    ),
    CONSTRAINT metric_daily_spread CHECK (
        min <= max AND value BETWEEN min AND max AND mean BETWEEN min AND max
    ),
    -- The morning value is the daily value exactly when the method is morning (PLAN 7.1 step 3).
    CONSTRAINT metric_daily_morning CHECK (
        (method = 'morning' AND morning IS NOT NULL AND value = morning
            AND cardinality(source_groups) = 1)
        OR (method = 'median' AND morning IS NULL)
    )
)"""

_FINDINGS = """
CREATE TABLE public.findings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    rule_id text NOT NULL CONSTRAINT findings_rule_id CHECK (rule_id ~ '^[A-Z][0-9]{1,2}$'),
    rule_version text NOT NULL CONSTRAINT findings_rule_version
        CHECK (rule_version ~ '^[a-z0-9][a-z0-9._/-]{0,39}$'),
    tier text NOT NULL
        CONSTRAINT findings_tier CHECK (tier IN ('device', 'trend', 'experimental', 'system')),
    severity text NOT NULL
        CONSTRAINT findings_severity CHECK (severity IN ('urgent', 'attention', 'info')),
    confidence text NOT NULL
        CONSTRAINT findings_confidence CHECK (confidence IN ('low', 'medium', 'high')),
    data_quality jsonb NOT NULL DEFAULT '{}'::jsonb
        CONSTRAINT findings_data_quality CHECK (jsonb_typeof(data_quality) = 'object'),
    origin text NOT NULL CONSTRAINT findings_origin CHECK (origin IN ('live', 'backfill')),
    metric_keys text[] NOT NULL DEFAULT '{}'::text[] CONSTRAINT findings_metric_keys CHECK (
        cardinality(metric_keys) <= 16 AND array_position(metric_keys, NULL) IS NULL
    ),
    subject text NOT NULL
        CONSTRAINT findings_subject CHECK (subject ~ '^[A-Za-z0-9_.:/-]{1,160}$'),
    window_start date NOT NULL,
    window_end date NOT NULL,
    reference_day date,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb
        CONSTRAINT findings_evidence CHECK (jsonb_typeof(evidence) = 'object'),
    input_revision bigint NOT NULL CONSTRAINT findings_input_revision CHECK (input_revision >= 0),
    input_fingerprint bytea NOT NULL CONSTRAINT findings_input_fingerprint
        CHECK (octet_length(input_fingerprint) = 32),
    status text NOT NULL DEFAULT 'active'
        CONSTRAINT findings_status CHECK (status IN ('active', 'ended')),
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    notify_state text NOT NULL DEFAULT 'none'
        CONSTRAINT findings_notify_state CHECK (notify_state IN ('none', 'pending', 'held', 'sent')),
    notified_at timestamptz,
    seen_at timestamptz,
    CONSTRAINT findings_owner_id_id_key UNIQUE (owner_id, id),
    CONSTRAINT findings_window CHECK (window_end >= window_start),
    CONSTRAINT findings_seen CHECK (last_seen >= first_seen),
    CONSTRAINT findings_ended CHECK ((status = 'ended') = (ended_at IS NOT NULL)),
    -- Amendment 3: the system tier is Q1's, and Q1 is always system.
    CONSTRAINT findings_system_tier CHECK ((tier = 'system') = (rule_id = 'Q1')),
    -- ADR 0010 section 5: device and system findings are stored high.
    CONSTRAINT findings_reported_confidence CHECK (
        tier NOT IN ('device', 'system') OR confidence = 'high'
    ),
    -- PLAN 7.1 step 7: backfill never notifies.
    CONSTRAINT findings_backfill_quiet CHECK (origin = 'live' OR notify_state = 'none'),
    CONSTRAINT findings_notified CHECK ((notify_state = 'sent') = (notified_at IS NOT NULL))
)"""

_ACTIVE_EPISODE = """
CREATE UNIQUE INDEX findings_active_episode
    ON public.findings (owner_id, rule_id, subject) WHERE status = 'active'
"""

_SCORES = f"""
CREATE TABLE public.scores (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    kind text NOT NULL CONSTRAINT scores_kind CHECK (kind ~ {_KEY}),
    day date NOT NULL,
    value numeric NOT NULL CONSTRAINT scores_value CHECK ({_FINITE.format(col="value")}),
    components jsonb NOT NULL DEFAULT '{{}}'::jsonb
        CONSTRAINT scores_components CHECK (jsonb_typeof(components) = 'object'),
    {_provenance("scores")}
    CONSTRAINT scores_owner_id_id_key UNIQUE (owner_id, id),
    CONSTRAINT scores_day_key UNIQUE (owner_id, kind, day)
)"""

_PROFILES = f"""
CREATE TABLE public.user_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    birth_year smallint CONSTRAINT user_profiles_birth_year CHECK (birth_year BETWEEN 1900 AND 2100),
    sex text CONSTRAINT user_profiles_sex CHECK (sex IN ('female', 'male')),
    day_assignment text NOT NULL DEFAULT 'reading_tz'
        CONSTRAINT user_profiles_day_assignment CHECK (day_assignment IN ('reading_tz', 'fixed_tz')),
    day_tz text,
    theme text NOT NULL DEFAULT 'system'
        CONSTRAINT user_profiles_theme CHECK (theme IN ('system', 'light', 'dark')),
    weight_unit text NOT NULL DEFAULT 'kg'
        CONSTRAINT user_profiles_weight_unit CHECK (weight_unit IN ('kg', 'lb', 'st')),
    temperature_unit text NOT NULL DEFAULT 'c'
        CONSTRAINT user_profiles_temperature_unit CHECK (temperature_unit IN ('c', 'f')),
    datetime_format text NOT NULL DEFAULT 'd_mon_y' CONSTRAINT user_profiles_datetime_format
        CHECK (datetime_format IN ('d_mon_y', 'mon_d_y', 'iso')),
    chart_range text NOT NULL DEFAULT '90d'
        CONSTRAINT user_profiles_chart_range CHECK (chart_range IN ('30d', '90d', '6mo', '1y')),
    overview_order text[] NOT NULL DEFAULT '{{body,heart,nerve,activity}}'::text[]
        CONSTRAINT user_profiles_overview_order CHECK (
            overview_order <@ ARRAY['body', 'heart', 'nerve', 'activity']::text[]
            AND cardinality(overview_order) <= 4
            AND array_position(overview_order, NULL) IS NULL
        ),
    show_unsupported boolean NOT NULL DEFAULT true,
    show_experimental boolean NOT NULL DEFAULT false,
    notify_email boolean NOT NULL DEFAULT false,
    notify_weekly boolean NOT NULL DEFAULT false,
    notify_tiers text[] NOT NULL DEFAULT '{{device,trend,experimental,system}}'::text[]
        CONSTRAINT user_profiles_notify_tiers CHECK (
            notify_tiers <@ ARRAY['device', 'trend', 'experimental', 'system']::text[]
            AND array_position(notify_tiers, NULL) IS NULL
        ),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT user_profiles_owner_id_id_key UNIQUE (owner_id, id),
    CONSTRAINT user_profiles_owner_key UNIQUE (owner_id),
    CONSTRAINT user_profiles_day_tz CHECK (
        (day_assignment = 'reading_tz' AND day_tz IS NULL)
        OR (day_assignment = 'fixed_tz' AND day_tz IS NOT NULL AND {_TZ.format(col="day_tz")})
    )
)"""

_CREATE = (
    _OBSERVATIONS,
    "CREATE INDEX observations_ts ON public.observations USING brin (ts)",
    _DAILY,
    _FINDINGS,
    _ACTIVE_EPISODE,
    _SCORES,
    _PROFILES,
)


def upgrade() -> None:
    for statement in _CREATE:
        op.execute(statement)
    for table in _OWNED:
        enable_owner_rls(table)


def downgrade() -> None:
    for table in reversed(_OWNED):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
