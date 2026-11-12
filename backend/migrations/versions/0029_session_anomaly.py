"""accounts: the session anomaly signal's per-session state (#461, ADR 0018)

Revision ID: 0029_session_anomaly
Revises: 0028_chart_range_7d
Create Date: 2026-11-12

``sessions`` gains what ``auth/session_anomaly.py`` keeps per session to score a request
against its history (``auth/session_anomaly_policy.py``):

- ``seen_window_at`` and ``seen_count``: the fixed one-minute request counter;
- ``seen_country``: the country of the session's first request that had one (only with a geo
  source; ISO 3166-1 alpha-2, or Cloudflare's ``T1``);
- ``anomaly_at``: when the session's one ``session_anomaly`` event was written, so a session is
  flagged at most once.

All four go with the session row: a sign-out, a rotation (a new row), an expiry or an account
delete removes them. The event itself is an ``auth_events`` row (a new ``kind``, no schema
change: the column has no check) and is purged after 30 days like every other one.

The partial index serves the admin abuse view (``admin_session_anomalies``,
``docker/postgres/maint/70-admin-accounts.sql``): the anomaly rows of every user, newest first,
paged by ``(at, id)``.

System tables (ADR 0002 section 3): no ``owner_id`` and no RLS, so no policy changes; the app
role's DML grants already cover a new column.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_session_anomaly"
down_revision: str | Sequence[str] | None = "0028_chart_range_7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = {
    "seen_window_at": "timestamptz",
    "seen_count": (
        "integer NOT NULL DEFAULT 0 CONSTRAINT sessions_seen_count CHECK (seen_count >= 0)"
    ),
    "seen_country": (
        "text CONSTRAINT sessions_seen_country CHECK (seen_country ~ '^[A-Z0-9]{2}$')"
    ),
    "anomaly_at": "timestamptz",
}
INDEX = "auth_events_anomaly_at_idx"


def upgrade() -> None:
    for column, definition in COLUMNS.items():
        op.execute(f"ALTER TABLE public.sessions ADD COLUMN {column} {definition}")
    op.execute(
        f"CREATE INDEX {INDEX} ON public.auth_events (at, id) WHERE kind = 'session_anomaly'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS public.{INDEX}")
    for column in reversed(COLUMNS):
        op.execute(f"ALTER TABLE public.sessions DROP COLUMN IF EXISTS {column}")
