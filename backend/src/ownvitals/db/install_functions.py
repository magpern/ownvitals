"""Install the database functions as the superuser: the runner of compose's ``db-functions``.

``docker/postgres/queue/db-functions.sh`` writes the superuser's password to a ``PGPASSFILE`` on a
tmpfs, then runs::

    python -m ownvitals.db.install_functions "host=db port=5432 dbname=ownvitals user=postgres" \\
        /app/db-functions/20-queue.sql

The SQL files ship in the release image (``Dockerfile``), never on a host path, so a deploy
installs that release's functions and a rollback re-installs the previous release's
(``docs/dev/deploy.md``). The files are plain SQL (no psql meta-commands): all of them are sent in
ONE transaction, as ``psql --single-transaction -v ON_ERROR_STOP=1`` runs them, and the first
error rolls everything back. A file that controls the transaction itself (``COMMIT``,
``ROLLBACK``, ``BEGIN``, ``END``, savepoints, ...) is refused before connecting, since it could
commit part of the work.

The password reaches libpq only through ``PGPASSFILE``, which must name a file: a connection
string that carries a password (``password``, ``passfile``) is refused, and so is every other
``PG*`` environment variable (``PGPASSWORD``, ``PGSERVICE``, ``PGOPTIONS``, ...), which libpq
would read too. The password never appears in an argument, the environment or the output.
Imports nothing from the application but psycopg; reads no settings.
"""

import os
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# What the connection string may set: where to connect and as whom, never a credential.
ALLOWED_KEYS = frozenset({"host", "port", "dbname", "user", "connect_timeout", "sslmode"})
CONNECT_TIMEOUT_SECONDS = 10
# The one libpq environment variable the runner accepts.
ALLOWED_ENVIRONMENT = frozenset({"PGPASSFILE"})
# Statements that end, open or split the transaction (first keyword of a top-level statement).
TRANSACTION_CONTROL = frozenset(
    {"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "PREPARE"}
)


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
    """The password comes from a PGPASSFILE that exists; no other PG* variable is set (libpq
    reads them all: PGPASSWORD, a service file, options, another host)."""
    other = sorted(
        k for k in environ if k.upper().startswith("PG") and k not in ALLOWED_ENVIRONMENT
    )
    if other:
        raise InstallError(f"{', '.join(other)} is set; only PGPASSFILE may be")
    passfile = environ.get("PGPASSFILE", "")
    if not passfile or not Path(passfile).is_file():
        raise InstallError("PGPASSFILE is not set or is not a file")


_TOKEN = re.compile(
    r"""
      (?P<line>--[^\n]*)
    | (?P<block>/\*)
    | (?P<dollar>\$(?:[A-Za-z_\x80-￿][\w\x80-￿]*)?\$)
    | (?P<estring>[Ee]')
    | (?P<string>')
    | (?P<ident>")
    | (?P<semicolon>;)
    | (?P<word>[A-Za-z_][\w$]*)
    """,
    re.VERBOSE,
)


def _skip_block_comment(sql: str, at: int) -> int:
    """The index after a (nested) ``/* */`` comment that opens at ``at``."""
    depth, i = 1, at + 2
    while depth:
        close, opened = sql.find("*/", i), sql.find("/*", i)
        if close < 0:
            raise InstallError("an SQL file has an unterminated comment")
        if 0 <= opened < close:
            depth, i = depth + 1, opened + 2
        else:
            depth, i = depth - 1, close + 2
    return i


def _skip_quoted(sql: str, at: int, quote: str, backslash: bool) -> int:
    """The index after a literal or identifier whose opening quote is at ``at``; a doubled
    quote stays inside it, and so does a backslash escape in an ``E''`` string."""
    i = at + 1
    while True:
        j = sql.find(quote, i)
        if j < 0:
            raise InstallError("an SQL file has an unterminated quote")
        if backslash and sql.count("\\", i, j) and _escaped(sql, i, j):
            i = j + 1
            continue
        if sql.startswith(quote * 2, j):
            i = j + 2
            continue
        return j + 1


def _escaped(sql: str, start: int, j: int) -> bool:
    """True when the quote at ``j`` follows an odd run of backslashes."""
    run, k = 0, j - 1
    while k >= start and sql[k] == "\\":
        run, k = run + 1, k - 1
    return run % 2 == 1


def statement_keywords(sql: str) -> Iterator[str]:
    """The first keyword of each top-level statement, upper case. Comments, string literals,
    quoted identifiers and dollar-quoted bodies (function bodies, ``DO`` blocks) are skipped, so
    a ``BEGIN`` or ``END`` inside PL/pgSQL is not a statement here."""
    i, first = 0, True
    while (match := _TOKEN.search(sql, i)) is not None:
        kind, i = match.lastgroup, match.end()
        if kind == "block":
            i = _skip_block_comment(sql, match.start())
        elif kind == "dollar":
            close = sql.find(match.group(), i)
            if close < 0:
                raise InstallError("an SQL file has an unterminated dollar quote")
            i, first = close + len(match.group()), False
        elif kind in ("estring", "string", "ident"):
            quote = '"' if kind == "ident" else "'"
            i, first = _skip_quoted(sql, i - 1, quote, kind == "estring"), False
        elif kind == "semicolon":
            first = True
        elif kind == "word":
            if first:
                yield match.group().upper()
            first = False


def check_no_transaction_control(sql: str) -> None:
    found = sorted({word for word in statement_keywords(sql) if word in TRANSACTION_CONTROL})
    if found:
        raise InstallError(f"an SQL file controls the transaction itself ({', '.join(found)})")


def read_scripts(paths: Sequence[str]) -> str:
    if not paths:
        raise InstallError("no SQL file given")
    parts = []
    for name in paths:
        path = Path(name)
        if path.suffix != ".sql" or not path.is_file():
            raise InstallError(f"{name} is not an SQL file")
        parts.append(path.read_text(encoding="utf-8"))
    sql = "\n".join(parts)
    check_no_transaction_control(sql)
    return sql


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
        # which only libpq reads from the PGPASSFILE. Nothing is committed: the scripts hold no
        # transaction control (refused above), and the transaction rolled back.
        print(f"db-functions: failed, nothing was changed: {error}".rstrip(), file=sys.stderr)
        return 1
    print(f"db-functions: installed {', '.join(Path(p).name for p in argv[1:])}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
