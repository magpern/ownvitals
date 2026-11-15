"""#284: compose's ``migrate`` (``alembic upgrade head``) installs the egress hook (ADR 0009).

``migrations/env.py`` is the process's entry point: run from alembic's command line it installs
the hook with kind ``migrate`` (the configured database hosts only) before the engine connects.
The hook cannot be removed, so the command line runs in fresh interpreters
(``egress_migrate_child.py``); in this process ``conftest._egress_hook_stays_out`` keeps it
out, and an in-process ``alembic.command`` run installs none. The static guard scans ``env.py``
as ``migrations.env``. Hosts are synthetic.
"""

import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from psycopg.conninfo import conninfo_attempts, conninfo_attempts_async
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from egress_scan import HOOK_INSTALLERS, MIGRATIONS_ENV, scan_source, sources
from ownvitals.config import Settings
from ownvitals.egress import installed_kind
from ownvitals.egress.hook import _policy_for  # pyright: ignore[reportPrivateUsage]

BACKEND = Path(__file__).resolve().parent.parent
SRC = BACKEND / "src"
ENV_PY = BACKEND / "migrations" / "env.py"
CHILD = Path(__file__).resolve().parent / "egress_migrate_child.py"
INI = str(BACKEND / "alembic.ini")
APP_HOST, OWNER_HOST, MAIL_HOST = (
    "app-db.synthetic.invalid",
    "owner-db.synthetic.invalid",
    "mail.synthetic.invalid",
)
SYNTHETIC = {
    "OWNVITALS_DATABASE_URL": f"postgresql+psycopg://synthetic_app@{APP_HOST}:5432/ownvitals",
    "OWNVITALS_MIGRATIONS_DATABASE_URL": (
        f"postgresql+psycopg://synthetic_owner@{OWNER_HOST}:5432/ownvitals"
    ),
    "OWNVITALS_SMTP_HOST": MAIL_HOST,
}


def _child(args: list[str], values: dict[str, str], secrets: Path) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OWNVITALS_")}
    env |= values | {"OWNVITALS_SECRETS_DIR": str(secrets)}
    child = subprocess.run(  # noqa: S603 - our own interpreter and script, fixed arguments
        [sys.executable, str(CHILD), "-c", INI, *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=BACKEND,
        timeout=180,
        check=False,
    )
    assert child.returncode == 0, child.stderr[-3000:]
    return {"stdout": child.stdout, **json.loads(child.stdout.strip().splitlines()[-1])}


def _refuses_everything_else(report: dict[str, Any]) -> None:
    assert report["kind"] == "migrate"
    assert report["lookup"] == "socket.getaddrinfo"
    assert report["connect"] == "socket.connect"
    assert report["process"] == "subprocess.Popen"


def test_alembic_upgrade_from_the_command_line_installs_the_migrate_hook(
    tmp_path: Path,
) -> None:
    """Offline (``--sql``) runs every revision through ``env.py`` without connecting."""
    report = _child(["upgrade", "head", "--sql"], SYNTHETIC, tmp_path)
    assert "CREATE TABLE alembic_version" in report["stdout"]
    _refuses_everything_else(report)
    assert report["hosts"] == sorted({APP_HOST, OWNER_HOST})  # no SMTP, Google or Withings


def test_alembic_online_connects_to_the_database_under_the_hook(
    tmp_path: Path, owner_database_url: str, app_database_url: str
) -> None:
    """The hook does not stop migrate's own connection: psycopg resolves the database host in
    Python (allowed: a configured database host) and libpq connects natively."""
    values = {
        "OWNVITALS_DATABASE_URL": app_database_url,
        "OWNVITALS_MIGRATIONS_DATABASE_URL": owner_database_url,
    }
    report = _child(["current"], values, tmp_path)
    _refuses_everything_else(report)
    host = make_url(owner_database_url).host
    assert host is not None
    assert host.lower() in report["hosts"]


def test_an_in_process_alembic_run_installs_no_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """``alembic.command`` (the tests' migrations) runs inside a process that has its own hook,
    or, here, none: ``env.py`` installs only for alembic's command line."""
    for name, value in SYNTHETIC.items():
        monkeypatch.setenv(name, value)
    out = io.StringIO()
    command.upgrade(Config(INI, stdout=out), "head", sql=True)
    assert installed_kind() is None


def test_the_migrate_kind_resolves_only_the_database_hosts() -> None:
    settings = Settings(
        database_url=SecretStr(SYNTHETIC["OWNVITALS_DATABASE_URL"]),
        migrations_database_url=SecretStr(SYNTHETIC["OWNVITALS_MIGRATIONS_DATABASE_URL"]),
        smtp_host=MAIL_HOST,
    )
    policy = _policy_for("migrate", settings)
    assert policy.hosts == frozenset({APP_HOST, OWNER_HOST})
    assert policy.kind == "migrate"


def test_env_py_is_scanned_and_may_install_the_hook() -> None:
    assert (MIGRATIONS_ENV, ENV_PY.read_text(encoding="utf-8")) in sources(SRC)
    assert MIGRATIONS_ENV in HOOK_INSTALLERS
    source = ENV_PY.read_text(encoding="utf-8")
    assert 'install_egress_hook("migrate", settings)' in source
    assert "    require_standard_event_loop()" in source  # the online run's first statement
    assert scan_source(source, MIGRATIONS_ENV) == []
    # The same file anywhere else: it may not install the hook, nor use alembic.context.
    found = scan_source(source, "ownvitals.db.engine")
    assert any("only a process entry point installs the egress hook" in w for _, w in found)
    assert any("alembic.context" in w for _, w in found)


async def test_psycopg_looks_the_database_host_up_in_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0009 item 7 (#284): libpq's connect is native and unseen, but psycopg resolves the
    host through ``socket.getaddrinfo`` first, sync and async, and hands libpq the address, so
    an installed hook checks the host of migrate's (and db-functions') connection."""
    seen: list[str] = []

    def fake_getaddrinfo(host: str, port: object, *_: object, **__: object) -> list[Any]:
        seen.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 5432))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    params = {"host": OWNER_HOST, "port": "5432", "dbname": "ownvitals"}
    assert [a["hostaddr"] for a in conninfo_attempts(params)] == ["192.0.2.10"]
    assert [a["hostaddr"] for a in await conninfo_attempts_async(params)] == ["192.0.2.10"]
    assert seen == [OWNER_HOST, OWNER_HOST]
