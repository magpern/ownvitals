"""Setup for the job-table tests (ADR 0005): the migrated schema with the queue installed, and rows
written as the superuser (test setup only; it bypasses RLS so a test can plant any state,
including rows the application would never write, such as a poison row).

``queue_schema`` (module scope) migrates the test database to head as the owner role and runs
``docker/postgres/queue/20-queue.sql`` as the superuser, as compose's ``migrate`` and
``db-functions`` do. On teardown it **uninstalls the queue** (``DROP OWNED BY ownvitals_queue``,
then ``DROP ROLE``) and migrates back to base. The uninstall is a workaround (Architect, #77 item
2): guard 2 (``role_guard.py``) does not know the queue role and the app role's ``EXECUTE`` on the
queue functions yet, so they must not outlive these modules; guard 9 (``queue_guard.py``) checks
them exactly meanwhile. Synthetic data only: owners are random UUIDs (there is no ``users`` table
before #10), inputs are test-only.
"""

import hashlib
import os
import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection

from dbtools import migrated

BACKEND_DIR = Path(__file__).resolve().parent.parent
QUEUE_SCRIPT = BACKEND_DIR.parent / "docker" / "postgres" / "queue" / "20-queue.sql"
QUEUE_ROLE = "ownvitals_queue"
KIND = "test_kind"


def admin_url() -> str:
    """The test superuser's URL (``conftest``'s ``role_urls`` has checked it is loopback)."""
    return os.environ["OWNVITALS_TEST_DATABASE_URL"]


@contextmanager
def engine_for(url: str, **kwargs: Any) -> Iterator[Engine]:
    engine = create_engine(url, **kwargs)
    try:
        yield engine
    finally:
        engine.dispose()


def run_script(url: str, sql: str) -> None:
    """Plain SQL as the superuser in one transaction, as ``psql --single-transaction`` runs it
    (no parameters, so psycopg sends it as one simple query)."""
    with engine_for(url) as engine, engine.connect() as connection:
        raw = connection.connection.dbapi_connection
        assert raw is not None
        cursor = raw.cursor()
        try:
            cursor.execute(sql)  # pyright: ignore[reportArgumentType, reportCallIssue]
        finally:
            cursor.close()
        raw.commit()


def install_queue(url: str) -> None:
    run_script(url, QUEUE_SCRIPT.read_text(encoding="utf-8"))


def uninstall_queue(url: str) -> None:
    """Drop the queue functions and the role (the #77 workaround, see the module docstring)."""
    with (
        engine_for(url, isolation_level="AUTOCOMMIT") as engine,
        engine.connect() as connection,
    ):
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": QUEUE_ROLE}
        ).first()
        if exists is not None:
            connection.execute(text(f"DROP OWNED BY {QUEUE_ROLE} CASCADE"))
            connection.execute(text(f"DROP ROLE {QUEUE_ROLE}"))


@contextmanager
def queue_installed(owner_url: str) -> Iterator[str]:
    """The schema at head with the queue installed; removed afterwards. Yields the admin URL."""
    url = admin_url()
    uninstall_queue(url)  # a leftover of an interrupted run
    with migrated(owner_url):
        try:
            install_queue(url)
            yield url
        finally:
            uninstall_queue(url)


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def new_user() -> uuid.UUID:
    """A synthetic owner id; ``new_job`` and ``ensure_user`` give it its ``users`` row."""
    return uuid.uuid4()


def ensure_user(conn: Connection, owner: uuid.UUID) -> None:
    """The synthetic account of ``owner``: ``jobs.owner_id`` references ``users`` since #10."""
    conn.execute(
        text(
            "INSERT INTO users (id, email, email_normalized, email_verified_at) "
            "VALUES (:id, :email, :email, now()) ON CONFLICT (id) DO NOTHING"
        ),
        {"id": owner, "email": f"synthetic-{owner}@example.test"},
    )


def new_job(conn: Connection, owner: uuid.UUID, **columns: Any) -> uuid.UUID:
    """A queued ``test_kind`` job of ``owner`` (whose account it creates when missing)."""
    ensure_user(conn, owner)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "owner_id": owner,
        "kind": KIND,
        "state": "queued",
        "input": "{}",
        "idempotency_key": secrets.token_urlsafe(18),
        "request_hash": digest("request"),
        **columns,
    }
    names = ", ".join(values)
    params = ", ".join(f"CAST(:{k} AS jsonb)" if k == "input" else f":{k}" for k in values)
    return conn.execute(
        text(f"INSERT INTO jobs ({names}) VALUES ({params}) RETURNING id"),  # noqa: S608
        values,
    ).scalar_one()


def job(conn: Connection, job_id: uuid.UUID) -> Any:
    return conn.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).one()


def events(conn: Connection, job_id: uuid.UUID) -> list[tuple[int, str, dict[str, Any], int]]:
    rows = conn.execute(
        text(
            "SELECT seq, kind, payload, lease_version FROM job_events WHERE job_id = :id "
            "ORDER BY seq"
        ),
        {"id": job_id},
    ).all()
    return [(r.seq, r.kind, r.payload, r.lease_version) for r in rows]


def truncate_jobs(conn: Connection) -> None:
    conn.execute(text("TRUNCATE jobs, job_events"))


def as_user(conn: Connection, user: uuid.UUID) -> None:
    """The transaction-local ``app.user_id`` that ``user_session`` sets (ADR 0002 section 6)."""
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user)})


@pytest.fixture(scope="module", name="queue_schema")
def queue_schema_fixture(role_urls: Any) -> Iterator[str]:
    """Module scope: the migrated schema with the queue; yields the admin URL."""
    with queue_installed(role_urls.owner) as url:
        yield url


@pytest.fixture(name="admin")
def admin_fixture(queue_schema: str) -> Iterator[Connection]:
    """An autocommit superuser connection on empty job tables."""
    with (
        engine_for(queue_schema, isolation_level="AUTOCOMMIT") as engine,
        engine.connect() as connection,
    ):
        truncate_jobs(connection)
        yield connection
