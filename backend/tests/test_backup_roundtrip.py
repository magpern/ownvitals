"""The real round trip (issue #463): a synthetic database in a compose Postgres of the
production image, migrated to head with the db-functions scripts and synthetic owned rows, is
backed up by ``ownvitals-backup db`` (pg_dump through age) and restored by
``ownvitals-restore-drill`` into its own throwaway project; the drill's counts must equal the
source's, its Alembic revision must be head, its RLS check must pass, and it must tear down.

Docker and age are needed: without them the test fails in CI (``CI=true``) and is skipped
anywhere else. The keypairs are generated for the test; nothing here is real data or a real key.
"""

import re
import secrets
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from dbtools import (
    CHILD,
    PARENT,
    alembic_config,
    create_owned_tables,
    install_db_functions,
    unavailable,
)
from test_backup_harness import DRILL, ROOT, BackupHost, need, run

DB_IMAGE = re.findall(r"^readonly DB_IMAGE=(\S+)$", DRILL.read_text(encoding="utf-8"), re.M)[0]
SOURCE_COMPOSE = """services:
  db:
    image: {image}
    pull_policy: missing
    environment:
      POSTGRES_DB: ownvitals
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets:
      - postgres_password
      - database_owner_password
      - database_password
      - database_maint_password
    volumes:
      - {init}:/docker-entrypoint-initdb.d:ro
    ports:
      - "127.0.0.1::5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U postgres -d ownvitals"]
      interval: 2s
      timeout: 5s
      retries: 60
secrets:
  postgres_password: {{file: ./secrets/postgres_password}}
  database_owner_password: {{file: ./secrets/database_owner_password}}
  database_password: {{file: ./secrets/database_password}}
  database_maint_password: {{file: ./secrets/database_maint_password}}
"""
INSERT_PARENT = f"INSERT INTO {PARENT} (owner_id, name) VALUES (:o, :n) RETURNING id"  # noqa: S608
INSERT_CHILD = f"INSERT INTO {CHILD} (owner_id, parent_id) VALUES (:o, :p)"  # noqa: S608
COUNT_LINE = re.compile(r"^count (\S+) (\d+)$")
COUNT_SQL = """SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"""


def docker_available() -> None:
    need("docker")
    probe = run(["docker", "info"], timeout=60)
    if probe.returncode != 0:
        unavailable("the Docker daemon is not reachable; the backup round trip needs it")


def compose(host: BackupHost, *args: str, timeout: int = 300) -> str:
    return run(
        ["docker", "compose", "--project-directory", str(host.app), "-p", host.project, *args],
        check=True,
        text=True,
        timeout=timeout,
    ).stdout


def url(role: str, password: str, port: str) -> str:
    return f"postgresql+psycopg://{role}:{password}@127.0.0.1:{port}/ownvitals"


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[BackupHost, str]]:
    """The backup host with a running, migrated synthetic source database; its superuser URL."""
    docker_available()
    host = BackupHost(
        tmp_path, fake_docker=False, project=f"ownvitals-bktest-{uuid.uuid4().hex[:10]}"
    )
    passwords = {
        name: secrets.token_hex(16)
        for name in (
            "postgres_password",
            "database_owner_password",
            "database_password",
            "database_maint_password",
        )
    }
    for name, value in passwords.items():
        (host.secrets / name).write_text(value + "\n")
        (host.secrets / name).chmod(0o644)  # read by the container's postgres user
    (host.secrets / "connection_kek").write_text(secrets.token_hex(32) + "\n")
    (host.app / "compose.yaml").write_text(
        SOURCE_COMPOSE.format(image=DB_IMAGE, init=ROOT / "docker" / "postgres" / "init")
    )
    try:
        compose(host, "up", "--detach", "--wait", "--wait-timeout", "180")
        port = compose(host, "port", "db", "5432").strip().rsplit(":", 1)[1]
        admin = url("postgres", passwords["postgres_password"], port)
        owner = url("ownvitals_owner", passwords["database_owner_password"], port)
        monkeypatch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", owner)
        command.upgrade(alembic_config(), "head")
        install_db_functions(admin)
        create_owned_tables(owner)
        fill_synthetic_rows(admin)
        yield host, admin
    finally:
        run(
            [
                *("docker", "compose", "--project-directory", str(host.app), "-p", host.project),
                *("down", "--volumes", "--remove-orphans", "--timeout", "5"),
            ],
            timeout=300,
        )


def fill_synthetic_rows(admin: str) -> None:
    """Synthetic users and owned rows, written as the superuser (RLS does not apply to it)."""
    engine = create_engine(admin)
    try:
        with engine.begin() as connection:
            owners = [uuid.uuid4() for _ in range(3)]
            for owner in owners:
                connection.execute(
                    text(
                        "INSERT INTO users (id, email, email_normalized, email_verified_at) "
                        "VALUES (:id, :email, :email, now())"
                    ),
                    {"id": owner, "email": f"synthetic-{owner}@example.test"},
                )
            for i in range(5):
                parent = connection.execute(
                    text(INSERT_PARENT),
                    {"o": owners[i % 3], "n": f"synthetic-{i}"},
                ).scalar_one()
                for _ in range(i):
                    connection.execute(
                        text(INSERT_CHILD),
                        {"o": owners[i % 3], "p": parent},
                    )
    finally:
        engine.dispose()


def source_counts(admin: str) -> dict[str, int]:
    engine = create_engine(admin)
    try:
        with engine.connect() as connection:
            tables = connection.execute(text(COUNT_SQL)).scalars().all()
            return {
                table: connection.execute(
                    text(f'SELECT count(*) FROM public."{table}"')  # noqa: S608 (a catalog name)
                ).scalar_one()
                for table in tables
            }
    finally:
        engine.dispose()


def drill_project(output: str) -> str:
    found = re.findall(r"^ok    db: (ownvitals-drill-[0-9a-f]{12}) started", output, re.M)
    assert len(found) == 1, output
    return found[0]


def assert_torn_down(project: str) -> None:
    label = f"label=com.docker.compose.project={project}"
    for kind in (["ps", "-a"], ["volume", "ls"], ["network", "ls"]):
        left = run(["docker", *kind, "-q", "--filter", label], text=True, check=True).stdout
        assert left.strip() == "", (kind, project)


def test_a_backup_restores_with_the_same_counts_head_and_rls(
    source: tuple[BackupHost, str],
) -> None:
    host, admin = source
    expected = source_counts(admin)
    assert (expected[PARENT], expected[CHILD], expected["users"]) == (5, 10, 3)

    backup = host.run("db", timeout=300)
    assert backup.returncode == 0, backup.stdout + backup.stderr
    (backup_set,) = host.sets()
    assert host.key_archives() == []

    drill = host.run_drill(str(backup_set), str(host.db_identity))
    out = drill.stdout + drill.stderr
    assert drill.returncode == 0, out
    restored = {m[1]: int(m[2]) for line in out.splitlines() if (m := COUNT_LINE.match(line))}
    assert restored == expected
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert f"ok    alembic: current {head}\n" in drill.stdout
    assert re.search(
        r"^ok    rls: \d+ owned tables forced; ownvitals_app without a user sees 0 rows", out, re.M
    )
    assert re.search(r"^ok    rto: restored and checked in \d+s \(RTO 14400s\)$", out, re.M)
    assert_torn_down(drill_project(out))
    # Counts only: no synthetic value of a row reaches the output.
    assert "synthetic-" not in out
    assert "example.test" not in out

    # The wrong identity (the keys one) opens nothing, and the drill still tears down.
    wrong = host.run_drill(str(backup_set), str(host.keys_identity))
    assert wrong.returncode == 1
    assert "FAIL  restore: the roles did not restore" in wrong.stderr
    assert_torn_down(drill_project(wrong.stdout))

    # A backup of a table that lost forced RLS fails the drill's RLS check.
    engine = create_engine(admin)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {PARENT} NO FORCE ROW LEVEL SECURITY"))
    finally:
        engine.dispose()
    time.sleep(1.1)  # a new second: a new set name
    assert host.run("db", timeout=300).returncode == 0
    newest = host.sets()[-1]
    assert newest != backup_set
    broken = host.run_drill(str(newest), str(host.db_identity))
    assert broken.returncode == 1
    assert "lost forced row-level security" in broken.stderr
    assert_torn_down(drill_project(broken.stdout))
