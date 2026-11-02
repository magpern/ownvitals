"""Withings rate limiter tables (PLAN section 4.4, ADR 0005 section 8; #23)

Per-app token bucket, global in-flight slots and the fairness rows. All three are system
tables: no ``owner_id``, no row-level security, no privilege for the app role. The app role
reaches them only through the functions in ``docker/postgres/withings/rate.sql``, which this
upgrade installs as well (a deploy's ``db-functions`` step installs that file again).

The budget column cannot exceed 100 a minute. Slots are the four rows of the global cap.
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0010_withings_rate"
down_revision: str | Sequence[str] | None = "0009_withings_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RATE_SQL = Path(__file__).resolve().parents[3] / "docker" / "postgres" / "withings" / "rate.sql"
_IMAGE_RATE_SQL = Path("/app/db-functions/rate.sql")

_BUCKETS = """
CREATE TABLE public.withings_rate_buckets (
    app_id integer PRIMARY KEY REFERENCES public.withings_apps (id) ON DELETE CASCADE,
    remaining numeric(12, 4) NOT NULL,
    budget integer NOT NULL,
    refilled_at timestamptz NOT NULL,
    paused_until timestamptz,
    CONSTRAINT withings_rate_buckets_budget CHECK (budget BETWEEN 1 AND 100),
    CONSTRAINT withings_rate_buckets_remaining CHECK (
        remaining >= 0 AND remaining <= budget)
)"""

_SLOTS = """
CREATE TABLE public.withings_rate_slots (
    id smallint PRIMARY KEY,
    lease_id uuid,
    lease_until timestamptz,
    app_id integer,
    CONSTRAINT withings_rate_slots_id CHECK (id BETWEEN 1 AND 4),
    CONSTRAINT withings_rate_slots_lease_id_key UNIQUE (lease_id)
)"""

_DEMAND = """
CREATE TABLE public.withings_rate_demand (
    app_id integer NOT NULL REFERENCES public.withings_apps (id) ON DELETE CASCADE,
    account_id uuid NOT NULL,
    lane smallint NOT NULL,
    seen_at timestamptz NOT NULL,
    granted_at timestamptz,
    window_grants integer NOT NULL DEFAULT 0,
    window_started timestamptz NOT NULL,
    PRIMARY KEY (app_id, account_id, lane),
    CONSTRAINT withings_rate_demand_lane CHECK (lane BETWEEN 1 AND 5),
    CONSTRAINT withings_rate_demand_window CHECK (window_grants >= 0)
)"""

_SEED_SLOTS = """
INSERT INTO public.withings_rate_slots (id) VALUES (1), (2), (3), (4)
"""

_REVOKE = (
    "REVOKE ALL ON TABLE public.withings_rate_buckets FROM ownvitals_app, PUBLIC",
    "REVOKE ALL ON TABLE public.withings_rate_slots FROM ownvitals_app, PUBLIC",
    "REVOKE ALL ON TABLE public.withings_rate_demand FROM ownvitals_app, PUBLIC",
)

_DROP_FUNCTIONS = (
    "DROP FUNCTION IF EXISTS public.withings_rate_claim(integer, uuid, integer, integer)",
    "DROP FUNCTION IF EXISTS public.withings_rate_release(uuid)",
    "DROP FUNCTION IF EXISTS public.withings_rate_pause(integer, double precision)",
)


def _rate_sql() -> str:
    """The function script: the repo copy in tests, the image copy on a deploy."""
    for path in (_RATE_SQL, _IMAGE_RATE_SQL):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError("docker/postgres/withings/rate.sql")


def _install_rate_functions() -> None:
    sql = _rate_sql()
    if op.get_context().as_sql:
        op.execute(sql)
        return
    connection = op.get_bind()
    raw = connection.connection.dbapi_connection
    if raw is None:
        raise RuntimeError("the migration connection has no database driver")
    cursor = raw.cursor()
    try:
        cursor.execute(sql)  # pyright: ignore[reportArgumentType, reportCallIssue]
    finally:
        cursor.close()


def upgrade() -> None:
    for statement in (_BUCKETS, _SLOTS, _DEMAND, _SEED_SLOTS, *_REVOKE):
        op.execute(statement)
    _install_rate_functions()


def downgrade() -> None:
    for statement in _DROP_FUNCTIONS:
        op.execute(statement)
    op.execute("DROP TABLE IF EXISTS public.withings_rate_demand")
    op.execute("DROP TABLE IF EXISTS public.withings_rate_slots")
    op.execute("DROP TABLE IF EXISTS public.withings_rate_buckets")
