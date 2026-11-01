"""The db-functions runner refuses SQL that controls its own transaction (``COMMIT``,
``ROLLBACK``, ``BEGIN``, ...): such a file could commit part of the work before a later error,
while the runner promises one transaction. The scan skips comments, literals, quoted identifiers
and dollar-quoted bodies, so PL/pgSQL's ``BEGIN ... END`` and the words inside strings pass.

No database: the refusal comes before the runner connects."""

import os
from pathlib import Path

import pytest

from dbtools import BACKEND_DIR
from ownvitals.db.install_functions import (
    InstallError,
    check_no_transaction_control,
    main,
    statement_keywords,
)

QUEUE_SCRIPT = BACKEND_DIR.parent / "docker" / "postgres" / "queue" / "20-queue.sql"

REFUSED = [
    "COMMIT;",
    "commit",
    "SELECT 1; COMMIT;",
    "create table t2 (a int); commit; create table t3 (a int); select 1/0;",
    "SELECT 1;\n  Commit work;",
    "END;",
    "BEGIN;",
    "begin transaction isolation level serializable;",
    "START TRANSACTION;",
    "ROLLBACK;",
    "ABORT;",
    "SAVEPOINT a;",
    "RELEASE SAVEPOINT a;",
    "PREPARE TRANSACTION 'x';",
    "/* a comment */ COMMIT;",
    "-- a comment\nCOMMIT;",
    "create table t1 (); -- a note\rcommit; select 1/0;",  # a bare CR ends the comment
    "-- a note\r\nCOMMIT;",
    "/* nested /* twice */ still */ COMMIT;",
    "SELECT 'x';COMMIT;",
    "DO $$ BEGIN PERFORM 1; END $$; COMMIT;",
    "SELECT E'it\\'s'; COMMIT;",
    "SELECT 'it''s'; COMMIT;",
    'SELECT "a""b"; COMMIT;',
    "SELECT $q$ $$ $q$; COMMIT;",
]

ACCEPTED = [
    "SELECT 1;",
    "DO $$ BEGIN PERFORM 1; END $$;",
    "CREATE FUNCTION f() RETURNS int AS $f$ BEGIN RETURN 1; END; $f$ LANGUAGE plpgsql;",
    "SELECT 'commit; begin';",
    "SELECT E'it\\'s; commit';",
    "SELECT 'it''s; commit';",
    'SELECT "commit" FROM t;',
    "/* commit; */ SELECT 1;",
    "-- commit;\nSELECT 1;",
    "/* nested /* commit; */ commit; */ SELECT 1;",
    "SELECT 1 AS end_marker;",
    "COMMENT ON FUNCTION f() IS 'x';",
    "SELECT begin_at FROM t; SELECT x AS commit_ FROM t;",
    "SELECT $1;",
    "SELECT a$b FROM t;",
]


@pytest.mark.parametrize("sql", REFUSED)
def test_transaction_control_is_refused(sql: str) -> None:
    with pytest.raises(InstallError, match="controls the transaction"):
        check_no_transaction_control(sql)


@pytest.mark.parametrize("sql", ACCEPTED)
def test_the_same_words_elsewhere_are_accepted(sql: str) -> None:
    check_no_transaction_control(sql)


@pytest.mark.parametrize(
    "sql", ["SELECT 'open;", 'SELECT "open;', "/* open", "DO $$ BEGIN END;", "SELECT E'\\';"]
)
def test_an_unterminated_quote_or_comment_is_refused(sql: str) -> None:
    with pytest.raises(InstallError, match="unterminated"):
        check_no_transaction_control(sql)


def test_the_queue_script_holds_no_transaction_control() -> None:
    sql = QUEUE_SCRIPT.read_text(encoding="utf-8")
    keywords = list(statement_keywords(sql))
    assert keywords  # the scan reads statements at all
    assert set(keywords) <= {"DO", "ALTER", "REVOKE", "GRANT", "CREATE", "COMMENT", "SELECT"}, set(
        keywords
    )
    check_no_transaction_control(sql)


def test_the_runner_refuses_such_a_file_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    passfile = tmp_path / "pgpass"
    passfile.write_text("h:1:d:u:p\n")
    for name in [k for k in os.environ if k.upper().startswith("PG")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("PGPASSFILE", str(passfile))
    script = tmp_path / "split.sql"
    script.write_text("CREATE TABLE t2 (a int);\nCOMMIT;\nSELECT 1 / 0;\n")
    # Port 9 on loopback: a connection attempt would fail with exit 1, not refuse with 64.
    assert main(["host=127.0.0.1 port=9 dbname=d user=u", str(script)]) == 64
    assert "controls the transaction itself (COMMIT)" in capsys.readouterr().err
