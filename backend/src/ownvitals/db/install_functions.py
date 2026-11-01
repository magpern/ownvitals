"""Install the database functions as the superuser: the runner of compose's ``db-functions``.

``docker/postgres/queue/db-functions.sh`` writes the superuser's password to a ``PGPASSFILE`` on a
tmpfs, then runs::

    python -m ownvitals.db.install_functions "host=db port=5432 dbname=ownvitals user=postgres" \\
        /app/db-functions/20-queue.sql

The SQL files ship in the release image (``Dockerfile``), never on a host path, so a deploy
installs that release's functions and a rollback re-installs the previous release's
(``docs/dev/deploy.md``). The files are plain SQL (no psql meta-commands): all of them are sent in
ONE transaction, as ``psql --single-transaction -v ON_ERROR_STOP=1`` runs them, and the first
error rolls everything back.

The password reaches libpq only through ``PGPASSFILE``, which must name a file: a connection
string that carries a password (``password``, ``passfile``) is refused, so is a set
``PGPASSWORD``, and the password never appears in an argument, the
environment or the output. Imports nothing from the application but psycopg; reads no settings.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# What the connection string may set: where to connect and as whom, never a credential.
ALLOWED_KEYS = frozenset({"host", "port", "dbname", "user", "connect_timeout", "sslmode"})
CONNECT_TIMEOUT_SECONDS = 10


class InstallError(Exception):
    """Refused before connecting; the message names no secret."""


def check_conninfo(conninfo: str) -> dict[str, str]:
    try:
        parsed = conninfo_to_dict(conninfo)
    except psycopg.ProgrammingError:
        raise InstallError("the connection string is not valid") from None
    unknown = sorted(set(parsed) - ALLOWED_KEYS)
    if unknown:
        raise InstallError(f"the connection string may not set {', '.join(unknown)}")
    for key in ("host", "dbname", "user"):
        if not parsed.get(key):
            raise InstallError(f"the connection string must set {key}")
    return {key: str(value) for key, value in parsed.items()}


def check_environment(environ: Mapping[str, str]) -> None:
    """The password comes from a PGPASSFILE that exists, never from PGPASSWORD."""
    if "PGPASSWORD" in environ:
        raise InstallError("PGPASSWORD is set; the password goes in a PGPASSFILE only")
    passfile = environ.get("PGPASSFILE", "")
    if not passfile or not Path(passfile).is_file():
        raise InstallError("PGPASSFILE is not set or is not a file")


def read_scripts(paths: Sequence[str]) -> str:
    if not paths:
        raise InstallError("no SQL file given")
    parts = []
    for name in paths:
        path = Path(name)
        if path.suffix != ".sql" or not path.is_file():
            raise InstallError(f"{name} is not an SQL file")
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def install(conninfo: str, paths: Sequence[str]) -> None:
    """Run the SQL files in one transaction; any error rolls all of them back."""
    params = check_conninfo(conninfo)
    check_environment(os.environ)
    sql = read_scripts(paths)
    params.setdefault("connect_timeout", str(CONNECT_TIMEOUT_SECONDS))
    with psycopg.connect(make_conninfo(**params), autocommit=False) as connection:
        # No parameters: psycopg sends the text as one simple query, so several statements
        # (and dollar-quoted bodies) run as the server parses them. The block commits on
        # success and rolls back on an exception.
        connection.execute(sql)  # pyright: ignore[reportArgumentType, reportCallIssue]


def main(argv: Sequence[str]) -> int:
    if len(argv) < 2:
        print("usage: install_functions CONNINFO FILE.sql [FILE.sql ...]", file=sys.stderr)
        return 64
    try:
        install(argv[0], argv[1:])
    except InstallError as error:
        print(f"db-functions: refused: {error}", file=sys.stderr)
        return 64
    except psycopg.Error as error:
        # The server's message (an object name, the script's own RAISE); never a password,
        # which only libpq reads from the PGPASSFILE.
        print(f"db-functions: failed, nothing was changed: {error}".rstrip(), file=sys.stderr)
        return 1
    print(f"db-functions: installed {', '.join(Path(p).name for p in argv[1:])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
