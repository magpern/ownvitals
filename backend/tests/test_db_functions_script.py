"""The ``db-functions`` one-shot (ADR 0005 section 3): its entrypoint writes the superuser's
password to a ``PGPASSFILE`` that libpq reads back unchanged, and its runner
(``ownvitals.db.install_functions``) installs the SQL in one transaction with the password from
that file only.

The entrypoint runs under ``sh`` from a copy whose paths point into a temporary directory and
which stops before it starts the runner. The ``.pgpass`` line is checked twice: against
``libpq_password``, a model of libpq's ``passwordFromFile`` (PostgreSQL 17, ``fe-connect.c``), and,
with the test database, by authenticating a temporary role through libpq itself. Synthetic
passwords only."""

import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from dbtools import BACKEND_DIR, migrated
from jobtools import QUEUE_ROLE, QUEUE_SCRIPT, admin_url, uninstall_queue
from ownvitals.db import install_functions
from ownvitals.db.install_functions import InstallError, check_conninfo, check_environment
from test_deploy_harness import bash

ROOT = BACKEND_DIR.parent
SCRIPT = ROOT / "docker" / "postgres" / "queue" / "db-functions.sh"
ESCAPE = r"sed -e 's/\\/\\\\/g' -e 's/:/\\:/g'"  # as the script spells it
SWAPPED = r"sed -e 's/:/\\:/g' -e 's/\\/\\\\/g'"  # the mutation: colons first

PASSWORDS = [
    "plain-hex-0123abcd",
    "back\\slash",
    "co:lon",
    "a\\:b",  # a backslash before a colon
    "a:\\b",  # a colon before a backslash
    "\\\\::\\:",
    "trailing\\",
    ":leading",
    "spaces and 'quotes' \"too\" $HOME `x`",
]


# --- libpq's reading of a .pgpass line --------------------------------------------------------


def _field(line: str, start: int) -> tuple[str, int]:
    """One of the four leading fields: backslash escapes the next character, ':' ends it."""
    out, i = [], start
    while i < len(line) and line[i] != ":":
        if line[i] == "\\" and i + 1 < len(line):
            i += 1
        out.append(line[i])
        i += 1
    if i >= len(line):
        raise ValueError("fewer than five fields")
    return "".join(out), i + 1


def libpq_password(line: str) -> tuple[list[str], str]:
    """(host, port, database, user) and the password libpq takes from one ``.pgpass`` line.

    passwordFromFile drops the trailing newline (and a CR before it), matches the first four
    fields with their escapes, then de-escapes the rest up to the first unescaped ':'."""
    line = line.removesuffix("\n").removesuffix("\r")
    fields, i = [], 0
    for _ in range(4):
        value, i = _field(line, i)
        fields.append(value)
    password, rest = [], line[i:]
    j = 0
    while j < len(rest) and rest[j] != ":":
        if rest[j] == "\\" and j + 1 < len(rest):
            j += 1
        password.append(rest[j])
        j += 1
    return fields, "".join(password)


def test_the_libpq_model_reads_escapes_as_libpq_does() -> None:
    assert libpq_password("h:5432:d:u:p\\:w\\\\d\n") == (["h", "5432", "d", "u"], "p:w\\d")
    assert libpq_password("h:1:d:u:a:b\n")[1] == "a"  # an unescaped colon ends the password
    assert libpq_password("h:1:d:u:a\\\\:b\n")[1] == "a\\"
    with pytest.raises(ValueError, match="fewer than five"):
        libpq_password("h:1:d\n")


# --- the entrypoint, up to the runner ---------------------------------------------------------


def _script_copy(tmp_path: Path, escape: str = ESCAPE) -> Path:
    """The entrypoint with its two paths in ``tmp_path`` and without the final ``exec``, which
    becomes a print of PGPASSFILE (the script exported it)."""
    source = SCRIPT.read_text(encoding="utf-8")
    if escape != ESCAPE:  # a planted mutation; the script itself runs as it is
        assert source.count(ESCAPE) == 1
        source = source.replace(ESCAPE, escape)
    assert source.count("secret=/run/secrets/postgres_password\n") == 1
    assert source.count("pgpass=/run/pgpass/pgpass\n") == 1
    head, _, _ = source.partition("exec python -m ownvitals.db.install_functions")
    copy = (
        head.replace(
            "secret=/run/secrets/postgres_password", f"secret='{tmp_path.as_posix()}/secret'"
        ).replace("pgpass=/run/pgpass/pgpass", f"pgpass='{tmp_path.as_posix()}/pgpass'")
    ) + 'printf "%s\\n" "$PGPASSFILE"\n'
    path = tmp_path / "db-functions.sh"
    path.write_bytes(copy.encode())
    return path


def run_entrypoint(
    tmp_path: Path, secret: bytes, escape: str = ESCAPE
) -> subprocess.CompletedProcess[str]:
    script = _script_copy(tmp_path, escape)
    (tmp_path / "secret").write_bytes(secret)
    return subprocess.run(  # noqa: S603 - our own script, a synthetic secret
        [bash(), "--posix", str(script)] if sys.platform == "win32" else ["sh", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )


def pgpass_line(tmp_path: Path, password: str, escape: str = ESCAPE) -> str:
    run = run_entrypoint(tmp_path, password.encode() + b"\n", escape)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip().endswith("/pgpass")
    return (tmp_path / "pgpass").read_bytes().decode()


@pytest.mark.parametrize("password", PASSWORDS)
def test_libpq_reads_back_exactly_the_secret(tmp_path: Path, password: str) -> None:
    line = pgpass_line(tmp_path, password)
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert libpq_password(line) == (["db", "5432", "ownvitals", "postgres"], password)


@pytest.mark.parametrize("password", ["a\\:b", "co:lon", "a:\\b"])
def test_swapping_the_escape_steps_is_caught(tmp_path: Path, password: str) -> None:
    """The mutation the #96 review found: colons escaped first, then every backslash doubled."""
    line = pgpass_line(tmp_path, password, SWAPPED)
    assert libpq_password(line)[1] != password


def test_trailing_newlines_are_dropped_like_the_postgres_image(tmp_path: Path) -> None:
    run = run_entrypoint(tmp_path, b"abc\n\n\n")
    assert run.returncode == 0, run.stderr
    assert libpq_password((tmp_path / "pgpass").read_text())[1] == "abc"


# Git Bash's command substitution drops a CR before the final LF; dash (the image's sh) keeps it.
MSYS_DROPS_CR = pytest.mark.skipif(sys.platform == "win32", reason="Git Bash drops the CR")


@pytest.mark.parametrize(
    "secret",
    [
        b"",
        b"\n",
        b"\n\n",
        b"in\nside\n",
        pytest.param(b"carriage\r\n", marks=MSYS_DROPS_CR),
        b"a\rb",
    ],
)
def test_an_empty_secret_or_a_line_break_is_refused(tmp_path: Path, secret: bytes) -> None:
    run = run_entrypoint(tmp_path, secret)
    assert run.returncode == 1
    assert run.stdout == ""
    assert not (tmp_path / "pgpass").exists() or (tmp_path / "pgpass").read_bytes() == b""
    if secret.strip(b"\n"):
        assert "secret" in run.stderr
        assert secret.decode().strip() not in run.stderr  # never echoed


@pytest.mark.skipif(sys.platform == "win32", reason="file modes are POSIX only")
def test_the_pgpass_file_is_private(tmp_path: Path) -> None:
    pgpass_line(tmp_path, "x")
    assert (tmp_path / "pgpass").stat().st_mode & 0o777 == 0o600


def test_the_entrypoint_starts_the_runner_with_the_image_s_sql() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.rstrip("\n").endswith(
        "exec python -m ownvitals.db.install_functions \\\n"
        '  "host=db port=5432 dbname=ownvitals user=postgres" /app/db-functions/20-queue.sql'
    )
    assert "PGPASSWORD" not in source
    assert "psql" not in source


# --- the runner -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conninfo",
    [
        "host=db dbname=ownvitals user=postgres password=x",
        "host=db dbname=ownvitals user=postgres passfile=/tmp/p",
        "postgresql://postgres:x@db/ownvitals",
        "host=db dbname=ownvitals user=postgres options=-csearch_path=x",
        "host=db dbname=ownvitals",
        "dbname=ownvitals user=postgres",
        "host=db user=postgres",
        "not a conninfo '",
    ],
)
def test_a_connection_string_beyond_where_and_who_is_refused(conninfo: str) -> None:
    with pytest.raises(InstallError) as refused:
        check_conninfo(conninfo)
    assert "x@" not in str(refused.value)


def test_the_entrypoint_s_connection_string_is_accepted() -> None:
    assert check_conninfo("host=db port=5432 dbname=ownvitals user=postgres") == {
        "host": "db",
        "port": "5432",
        "dbname": "ownvitals",
        "user": "postgres",
    }


def test_the_password_comes_from_a_pgpassfile_only(tmp_path: Path) -> None:
    passfile = tmp_path / "pgpass"
    passfile.write_text("h:1:d:u:p\n")
    check_environment({"PGPASSFILE": str(passfile)})
    check_environment({"PGPASSFILE": str(passfile), "PATH": "/bin", "APP_PG": "x", "HOME": "/"})
    for environ in (
        {},
        {"PGPASSFILE": ""},
        {"PGPASSFILE": str(tmp_path / "missing")},
        {"PGPASSFILE": str(tmp_path)},
        {"PGPASSFILE": str(passfile), "PGPASSWORD": "p"},
        # Any other libpq variable: a service file can carry a password, options or a host.
        {"PGPASSFILE": str(passfile), "PGSERVICE": "x"},
        {"PGPASSFILE": str(passfile), "PGSERVICEFILE": str(passfile)},
        {"PGPASSFILE": str(passfile), "PGOPTIONS": "-c search_path=x"},
        {"PGPASSFILE": str(passfile), "PGHOST": "elsewhere"},
        {"PGPASSFILE": str(passfile), "PGSYSCONFDIR": str(tmp_path)},
        {"PGPASSFILE": str(passfile), "pgservice": "x"},  # Windows names are case-insensitive
    ):
        with pytest.raises(InstallError):
            check_environment(environ)


def test_the_runner_refuses_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    passfile = tmp_path / "pgpass"
    passfile.write_text("h:1:d:u:p\n")
    monkeypatch.setenv("PGPASSFILE", str(passfile))
    monkeypatch.delenv("PGPASSWORD", raising=False)
    conninfo = "host=127.0.0.1 port=9 dbname=d user=u"
    assert install_functions.main([conninfo]) == 64
    assert install_functions.main([conninfo, str(passfile)]) == 64  # not an .sql file
    assert install_functions.main([conninfo, str(tmp_path / "missing.sql")]) == 64
    monkeypatch.setenv("PGPASSWORD", "secret-value")
    script = tmp_path / "a.sql"
    script.write_text("SELECT 1;\n")
    assert install_functions.main([conninfo, str(script)]) == 64
    err = capsys.readouterr().err
    assert "secret-value" not in err
    assert err.count("db-functions: refused:") == 3


# --- with the test database: libpq itself -----------------------------------------------------


def _libpq_target() -> tuple[str, str, str]:
    url = make_url(admin_url())
    return url.host or "127.0.0.1", str(url.port or 5432), url.database or "ownvitals"


@pytest.fixture
def scratch_role(database_url: str) -> Iterator[tuple[str, str]]:
    """A login role with a password full of backslashes and colons; dropped afterwards."""
    role = f"test_pgpass_{secrets.token_hex(4)}"
    password = f"{secrets.token_hex(4)}\\:a:b\\\\c\\"
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            quoted = connection.execute(text("SELECT quote_literal(:p)"), {"p": password}).scalar()
            connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD {quoted}"))
            connection.execute(text(f"GRANT CONNECT ON DATABASE {_libpq_target()[2]} TO {role}"))
        yield role, password
    finally:
        with engine.connect() as connection:
            connection.execute(text(f"DROP OWNED BY {role}"))
            connection.execute(text(f"DROP ROLE IF EXISTS {role}"))
        engine.dispose()


def _connect_with_passfile(passfile: Path, role: str) -> None:
    import psycopg

    host, port, dbname = _libpq_target()
    with psycopg.connect(
        host=host, port=port, dbname=dbname, user=role, passfile=str(passfile), connect_timeout=10
    ) as connection:
        assert connection.execute("SELECT current_user").fetchone() == (role,)


def test_libpq_authenticates_with_the_written_line(
    tmp_path: Path, scratch_role: tuple[str, str]
) -> None:
    """The line the entrypoint writes, with this database's host, port, name and the role in
    place of the compose ones, signs in; with the swapped escaping it does not."""
    import psycopg

    role, password = scratch_role
    host, port, dbname = _libpq_target()

    def passfile(escape: str) -> Path:
        line = pgpass_line(tmp_path, password, escape)
        target = tmp_path / f"pgpass-{len(escape)}-{escape == ESCAPE}"
        target.write_text(
            line.replace("db:5432:ownvitals:postgres:", f"{host}:{port}:{dbname}:{role}:", 1)
        )
        if sys.platform != "win32":
            target.chmod(0o600)
        return target

    _connect_with_passfile(passfile(ESCAPE), role)
    with pytest.raises(psycopg.OperationalError):  # the server checks passwords at all
        _connect_with_passfile(passfile(SWAPPED), role)


# --- with the test database: the runner installs in one transaction ---------------------------


@pytest.fixture
def migrated_without_queue(owner_database_url: str) -> Iterator[str]:
    url = admin_url()
    uninstall_queue(url)
    with migrated(owner_database_url):
        try:
            yield url
        finally:
            uninstall_queue(url)


def run_runner(tmp_path: Path, url: str, *files: Path) -> subprocess.CompletedProcess[str]:
    parsed = make_url(url)
    host, port, dbname = _libpq_target()
    passfile = tmp_path / "runner-pgpass"
    escaped = (parsed.password or "").replace("\\", "\\\\").replace(":", "\\:")
    passfile.write_text(f"{host}:{port}:{dbname}:{parsed.username}:{escaped}\n")
    if sys.platform != "win32":
        passfile.chmod(0o600)
    environ = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    environ["PGPASSFILE"] = str(passfile)
    conninfo = f"host={host} port={port} dbname={dbname} user={parsed.username}"
    return subprocess.run(  # noqa: S603 - our own module
        [sys.executable, "-m", "ownvitals.db.install_functions", conninfo, *map(str, files)],
        capture_output=True,
        text=True,
        env=environ,
        check=False,
    )


def _queue_role_exists(url: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            found = connection.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": QUEUE_ROLE}
            ).first()
            return found is not None
    finally:
        engine.dispose()


def test_the_runner_installs_the_queue_and_rolls_back_on_any_error(
    tmp_path: Path, migrated_without_queue: str
) -> None:
    url = migrated_without_queue
    password = make_url(url).password or ""
    broken = tmp_path / "broken.sql"
    broken.write_text("SELECT 1 / 0;\n")
    failed = run_runner(tmp_path, url, QUEUE_SCRIPT, broken)
    assert failed.returncode == 1
    assert "nothing was changed" in failed.stderr
    assert not password or password not in failed.stderr + failed.stdout
    assert not _queue_role_exists(url)  # the queue script's work went with the error

    for _ in (1, 2):  # idempotent, as on every start
        done = run_runner(tmp_path, url, QUEUE_SCRIPT)
        assert done.returncode == 0, done.stderr
        assert done.stdout == "db-functions: installed 20-queue.sql\n"
    assert _queue_role_exists(url)
    assert not password or password not in done.stdout + done.stderr
