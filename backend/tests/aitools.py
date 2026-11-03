"""Setup for the AI cost-cap tests (ADR 0014 section 7, #205): synthetic users and running jobs,
written as the superuser, and the four functions called as the app role, as the worker calls
them. Synthetic data only: owners are random UUIDs with ``example.test`` addresses, no content.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from jobtools import as_user, engine_for, new_job

RESERVE = "SELECT ai_reserve(:amount, :job, :user_cap, :global_cap)"
SETTLE = "SELECT ai_settle(:id, :actual)"
RELEASE = "SELECT ai_release(:id)"
SETTLE_LOST = "SELECT ai_settle_lost(:n)"
AI_TABLES = "ai_reservations, ai_spend, ai_spend_global"


def running_job(su: Connection, owner: uuid.UUID, *, lease_version: int = 1) -> uuid.UUID:
    """A ``test_kind`` job of ``owner`` that a worker holds, with a lease of ten minutes."""
    now = datetime.now(UTC)
    return new_job(
        su,
        owner,
        state="running",
        attempt=1,
        lease_version=lease_version,
        leased_by="synthetic-worker",
        lease_expires_at=now + timedelta(minutes=10),
        heartbeat_at=now,
        started_at=now,
    )


def counters(su: Connection, table: str, **where: Any) -> tuple[int, int]:
    """(reserved, spent) of one row of ``ai_spend`` or ``ai_spend_global``; (0, 0) if absent."""
    clause = " AND ".join(f"{k} = :{k}" for k in where) or "true"
    row = su.execute(
        text(f"SELECT reserved, spent FROM {table} WHERE {clause}"),  # noqa: S608 - test names
        where,
    ).first()
    return (0, 0) if row is None else (int(row.reserved), int(row.spent))


def reservation(su: Connection, reservation_id: uuid.UUID | None) -> Any:
    assert reservation_id is not None, "the reservation was refused"
    return su.execute(
        text("SELECT * FROM ai_reservations WHERE id = :id"), {"id": reservation_id}
    ).one()


@contextmanager
def app_engine(app_database_url: str) -> Iterator[Engine]:
    """The app role, one fresh connection per use (no pool: threads get their own)."""
    with engine_for(app_database_url, poolclass=NullPool) as engine:
        yield engine


def call(engine: Engine, user: uuid.UUID | None, sql: str, **params: Any) -> Any:
    """One transaction as the app role, ``app.user_id`` set to ``user`` (None: no context)."""
    with engine.begin() as conn:
        if user is not None:
            as_user(conn, user)
        return conn.execute(text(sql), params).scalar_one()


def reserve(
    engine: Engine,
    user: uuid.UUID,
    job: uuid.UUID,
    amount: int,
    *,
    user_cap: int,
    global_cap: int,
) -> uuid.UUID | None:
    return call(
        engine, user, RESERVE, amount=amount, job=job, user_cap=user_cap, global_cap=global_cap
    )


def ledger_matches_counters(su: Connection) -> None:
    """Every counter row equals what its reservations add up to (no lost or double update)."""
    rows = su.execute(
        text(
            "SELECT s.owner_id, s.month, s.reserved, s.spent, "
            "coalesce(sum(r.amount) FILTER (WHERE r.state = 'reserved'), 0) AS want_reserved, "
            "coalesce(sum(r.actual) FILTER (WHERE r.state = 'settled'), 0) AS want_spent "
            "FROM ai_spend s LEFT JOIN ai_reservations r "
            "ON r.owner_id = s.owner_id AND r.month = s.month "
            "GROUP BY s.owner_id, s.month, s.reserved, s.spent"
        )
    ).all()
    for row in rows:
        assert (row.reserved, row.spent) == (row.want_reserved, row.want_spent)
    months = su.execute(
        text(
            "SELECT g.month, g.reserved, g.spent, "
            "(SELECT coalesce(sum(s.reserved), 0) FROM ai_spend s WHERE s.month = g.month) AS r, "
            "(SELECT coalesce(sum(s.spent), 0) FROM ai_spend s WHERE s.month = g.month) AS s "
            "FROM ai_spend_global g"
        )
    ).all()
    for month in months:
        assert (month.reserved, month.spent) == (month.r, month.s)


@pytest.fixture(name="ai")
def ai_fixture(admin: Connection) -> Connection:
    """The superuser connection on empty AI tables (and empty job tables, ``admin``)."""
    admin.execute(text(f"TRUNCATE {AI_TABLES}"))
    return admin
