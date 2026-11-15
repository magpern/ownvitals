"""The ``_ai_eval`` test database, prepared as the owner prepares the evaluation's (ADR 0014
section 9; #531, #527). Shared by ``test_ai_eval_seed_db.py`` and ``test_ai_eval_jobs_db.py``;
no tests here.

``eval_db`` (module scope) creates ``<test database>_ai_eval`` and prepares it: the roles
script, the migrations as the owner role, then the db-functions scripts as the superuser. The
function-owner roles may own objects in one database of a cluster only (each db-functions
script refuses otherwise), so a module using it needs the test database at base, as every other
module leaves it, and the fixture drops the database at the module's end. In the test cluster
the evaluation database shares its cluster with the test database: the cluster check
(``ownvitals.db.synthetic_jobs``) refuses that, which its tests use.

Synthetic accounts only.
"""

import os
import secrets
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from conftest import RoleUrls
from dbtools import ROLES_SCRIPT, alembic_config, install_db_functions, unavailable, url_for
from ownvitals.config import Settings
from ownvitals.context import UserContext
from ownvitals.db import create_engine as create_app_engine
from ownvitals.db import create_session_factory, user_session

SYNTHETIC = True


def open_tx_for(factory: Any, ctx: UserContext) -> Callable[[], AbstractAsyncContextManager[Any]]:
    """The context's transaction opener, as a job's fence hands it to the seeder."""

    def open_tx() -> AbstractAsyncContextManager[Any]:
        return user_session(factory, ctx)

    return open_tx


@dataclass(frozen=True)
class EvalDb:
    superuser: str
    owner: str
    app: str


def _run_roles_script(url: str, role_urls: RoleUrls, tmp: Path) -> None:
    psql = shutil.which("psql")
    if psql is None:
        unavailable("psql is needed to run 10-roles.sql")
    assert psql is not None
    (tmp / "owner").write_text(make_url(role_urls.owner).password or "", "utf-8")
    (tmp / "app").write_text(make_url(role_urls.app).password or "", "utf-8")
    (tmp / "maint").write_text(secrets.token_hex(16), "utf-8")
    parsed = make_url(url)
    result = subprocess.run(  # noqa: S603 - our own script, fixed arguments
        [
            psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", parsed.host or "", "-p",
            str(parsed.port or 5432), "-U", parsed.username or "", "-d", parsed.database or "",
            "-f", str(ROLES_SCRIPT),
        ],
        env={
            **os.environ,
            "DATABASE_OWNER_PASSWORD_FILE": str(tmp / "owner"),
            "DATABASE_PASSWORD_FILE": str(tmp / "app"),
            "DATABASE_MAINT_PASSWORD_FILE": str(tmp / "maint"),
            "PGPASSWORD": parsed.password or "",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


def drop_database(superuser: str, name: str) -> None:
    engine = create_engine(superuser, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


@pytest.fixture(name="eval_db", scope="module")
def eval_db_fixture(
    role_urls: RoleUrls, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[EvalDb]:
    """``<test database>_ai_eval``, prepared as the owner prepares the eval's database."""
    main = make_url(role_urls.owner)
    name = f"{main.database}_ai_eval"
    superuser = make_url(os.environ["OWNVITALS_TEST_DATABASE_URL"])
    main_superuser = superuser.render_as_string(hide_password=False)
    drop_database(main_superuser, name)
    engine = create_engine(main_superuser, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    eval_superuser = superuser.set(database=name).render_as_string(hide_password=False)
    db = EvalDb(
        superuser=eval_superuser,
        owner=url_for(eval_superuser, "ownvitals_owner", main.password or ""),
        app=url_for(eval_superuser, "ownvitals_app", make_url(role_urls.app).password or ""),
    )
    try:
        _run_roles_script(eval_superuser, role_urls, tmp_path_factory.mktemp("eval-roles"))
        patch = pytest.MonkeyPatch()
        patch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", db.owner)
        try:
            command.upgrade(alembic_config(), "head")
        finally:
            patch.undo()
        install_db_functions(eval_superuser)
        yield db
    finally:
        drop_database(main_superuser, name)


@pytest.fixture(name="eval_clean")
def eval_clean_fixture(eval_db: EvalDb) -> EvalDb:
    engine = create_engine(eval_db.superuser)
    try:
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE users CASCADE"))
    finally:
        engine.dispose()
    return eval_db


@pytest.fixture(name="owner_engine")
async def owner_engine_fixture(eval_clean: EvalDb) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(eval_clean.owner)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(name="factory")
async def factory_fixture(eval_clean: EvalDb) -> AsyncIterator[object]:
    engine = create_app_engine(Settings(database_url=eval_clean.app))  # pyright: ignore[reportArgumentType]
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()
