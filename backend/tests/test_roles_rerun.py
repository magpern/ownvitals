"""``docker/postgres/init/10-roles.sql`` run again after migrating to head: its blanket re-grant
of ``SELECT, INSERT, UPDATE, DELETE`` on every table is followed by the tables' own narrower
grants (``alembic_version``: none), so the app role ends with exactly what a plain migration
leaves, table and column privileges alike, and guard 2 (``check_roles``) passes. CI also runs the
script twice on the empty database (the ``backend`` job's roles step).

Its own module: the re-run touches every table, which must not leak into other tests."""

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

from conftest import RoleUrls
from dbtools import ROLES_SCRIPT, migrated, unavailable
from role_guard import check_roles


@pytest.fixture
def admin(role_urls: RoleUrls, database_url: str) -> Iterator[Connection]:
    """The migrated schema, and a superuser connection to inspect it."""
    with migrated(role_urls.owner):
        engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
        try:
            with engine.connect() as connection:
                yield connection
        finally:
            engine.dispose()


def app_privileges(admin: Connection) -> set[tuple[str, str, str]]:
    """``(table, column or '*', privilege)`` the app role holds in schema public."""
    tables = admin.execute(
        text(
            "SELECT c.relname, '*', a.privilege_type FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(c.relacl) a "
            "WHERE n.nspname = 'public' AND a.grantee = CAST('ownvitals_app' AS regrole)"
        )
    ).all()
    columns = admin.execute(
        text(
            "SELECT c.relname, t.attname, a.privilege_type FROM pg_attribute t "
            "JOIN pg_class c ON c.oid = t.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace, "
            "aclexplode(t.attacl) a "
            "WHERE n.nspname = 'public' AND a.grantee = CAST('ownvitals_app' AS regrole)"
        )
    ).all()
    return {(r[0], r[1], r[2]) for r in (*tables, *columns)}


def run_roles_script(database_url: str, role_urls: RoleUrls, tmp_path: Path) -> None:
    psql = shutil.which("psql")
    if psql is None:
        unavailable("psql is needed to run 10-roles.sql")  # a skip locally, a failure in CI
    assert psql is not None
    # The passwords the tests use (role_urls set random ones), so the re-run changes none.
    (tmp_path / "owner").write_text(make_url(role_urls.owner).password or "", "utf-8")
    (tmp_path / "app").write_text(make_url(role_urls.app).password or "", "utf-8")
    url = make_url(database_url)
    result = subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [
            psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", url.host or "", "-p",
            str(url.port or 5432), "-U", url.username or "", "-d", url.database or "", "-f",
            str(ROLES_SCRIPT),
        ],
        env={
            **os.environ,
            "DATABASE_OWNER_PASSWORD_FILE": str(tmp_path / "owner"),
            "DATABASE_PASSWORD_FILE": str(tmp_path / "app"),
            "PGPASSWORD": url.password or "",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


def test_running_the_roles_file_again_keeps_the_migrated_privileges(
    admin: Connection, database_url: str, role_urls: RoleUrls, tmp_path: Path
) -> None:
    before = app_privileges(admin)
    assert not [p for p in before if p[0] == "alembic_version"]
    assert check_roles(admin) == []
    run_roles_script(database_url, role_urls, tmp_path)
    assert app_privileges(admin) == before
    assert check_roles(admin) == []


def test_running_the_roles_file_again_repairs_drift(
    admin: Connection, database_url: str, role_urls: RoleUrls, tmp_path: Path
) -> None:
    """What the script manages comes back as section 5 says after someone changed it."""
    drift = (
        "GRANT SELECT ON alembic_version TO ownvitals_app",
        "ALTER ROLE ownvitals_app BYPASSRLS",
        "ALTER ROLE ownvitals_app SET lock_timeout = '1h'",
        "GRANT CREATE ON SCHEMA public TO ownvitals_app, PUBLIC",
    )
    try:
        for statement in drift:
            admin.execute(text(statement))
        assert check_roles(admin) != []
        run_roles_script(database_url, role_urls, tmp_path)
        assert check_roles(admin) == []
    finally:  # leave nothing behind for the other tests, even when the script failed
        admin.execute(text("REVOKE ALL ON alembic_version FROM ownvitals_app"))
        admin.execute(text("ALTER ROLE ownvitals_app NOBYPASSRLS"))
        admin.execute(text("ALTER ROLE ownvitals_app SET lock_timeout = '5s'"))
        admin.execute(text("REVOKE CREATE ON SCHEMA public FROM ownvitals_app, PUBLIC"))
