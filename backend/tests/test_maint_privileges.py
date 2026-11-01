"""Guard 11 of ADR 0002 section 7 (#12): ``ownvitals_maint`` holds exactly its allowlist, can read
no secret column and no health table, and neither runtime role can change the audit log.

``docker/postgres/maint/30-maint.sql`` is run as the superuser on the schema at head (twice: it
repairs drift). The checks read the catalog through ``aclexplode`` and ``pg_shdepend``, so a
privilege of a kind nobody listed still fails, in any database of the cluster.
"""

import ast
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from admintools import MAINT_ROLE, MAINT_SCRIPT, admin_schema_fixture  # noqa: F401 - fixtures
from jobtools import admin_url, engine_for, run_script

SRC = Path(__file__).resolve().parent.parent / "src" / "ownvitals"

# The allowlist, as (object, privilege). Columns are ``table.column``; a table-level privilege is
# ``table`` alone. Every entry has its reason in 30-maint.sql.
AUDIT_COLUMNS = (
    *("id", "created_at", "actor_id", "actor_email", "action", "target_user_id"),
    *("target_kind", "target_id", "params", "outcome", "reason", "request_id"),
)
EXPECTED: frozenset[tuple[str, str]] = frozenset(
    {("database", "CONNECT"), ("schema public", "USAGE")}
    | {(f"admin_audit.{c}", "SELECT") for c in AUDIT_COLUMNS}
    | {("admin_audit", "INSERT"), ("admin_audit_id_seq", "USAGE")}
    | {
        (f"app_settings.{c}", "SELECT")
        for c in ("flag", "value", "created_at", "updated_at", "updated_by")
    }
    | {(f"app_settings.{c}", "INSERT") for c in ("flag", "value", "updated_by")}
    | {(f"app_settings.{c}", "UPDATE") for c in ("value", "updated_at", "updated_by")}
    | {
        (f"jobs.{c}", "SELECT")
        for c in ("id", "kind", "state", "error_category", "created_at", "finished_at")
    }
)

# Never readable by maint, whatever the allowlist says: secret-bearing columns (hashes,
# ciphertexts, nonces, wrapped keys, job input and keys, the audit row's IP HMAC) and whole tables.
SECRET_COLUMNS = {
    "users": {"password_hash"},
    "sessions": {"token_hash", "ip", "user_agent"},
    "known_devices": {"device_hash"},
    "pending_signups": {"token_hash", "password_hash", "browser_hash"},
    "auth_tokens": {"token_hash"},
    "oauth_flows": {"flow_hash", "state_hash", "nonce", "pkce_verifier"},
    "mfa_totp": {"secret_ciphertext", "nonce", "wrapped_dek", "dek_nonce"},
    "mfa_recovery_codes": {"code_hash"},
    "invites": {"code_hash"},
    "rate_limits": {"bucket_hash"},
    "jobs": {"input", "idempotency_key", "request_hash", "owner_id", "leased_by"},
    "admin_audit": {"ip_hash"},
}
# Tables maint may not touch at all: the account tables it has no view for (until #127), the
# key ring's check rows, the job events, and every health-data table of ADR 0002 section 4 by
# the names the specification gives them (each checked once it exists).
FORBIDDEN_TABLES = {
    "identities",
    "sessions",
    "known_devices",
    "pending_signups",
    "auth_tokens",
    "oauth_flows",
    "mfa_totp",
    "mfa_recovery_codes",
    "rate_limits",
    "auth_events",
    "invites",
    "users",
    "connection_keks",
    "job_events",
    "alembic_version",
    # Health data (PLAN section 5; ADR 0002 section 4): never, without a new ADR.
    "observations",
    "raw_payloads",
    "metric_daily",
    "findings",
    "scores",
    "estimated",
    "ecg_recordings",
    "ecg_signals",
    "webhook_events",
    "data_revisions",
}
WRITE_PRIVILEGES = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")

PRIVILEGES_QUERY = """
SELECT 'database', a.privilege_type FROM pg_database d, aclexplode(d.datacl) a
 WHERE d.datname = current_database() AND a.grantee = CAST(:r AS regrole)
UNION ALL
SELECT 'schema ' || n.nspname, a.privilege_type FROM pg_namespace n, aclexplode(n.nspacl) a
 WHERE a.grantee = CAST(:r AS regrole)
UNION ALL
SELECT c.relname, a.privilege_type FROM pg_class c, aclexplode(c.relacl) a
 WHERE a.grantee = CAST(:r AS regrole)
UNION ALL
SELECT c.relname || '.' || t.attname, a.privilege_type
  FROM pg_attribute t JOIN pg_class c ON c.oid = t.attrelid, aclexplode(t.attacl) a
 WHERE a.grantee = CAST(:r AS regrole)
UNION ALL
SELECT 'function ' || p.oid::regprocedure::text, a.privilege_type
  FROM pg_proc p, aclexplode(p.proacl) a WHERE a.grantee = CAST(:r AS regrole)
"""


def _privileges(conn: Connection) -> list[tuple[str, str]]:
    return [(str(o), str(p)) for o, p in conn.execute(text(PRIVILEGES_QUERY), {"r": MAINT_ROLE})]


@pytest.fixture(name="su")
def su_fixture(admin_schema: str) -> Any:
    del admin_schema
    with (
        engine_for(admin_url(), isolation_level="AUTOCOMMIT") as engine,
        engine.connect() as connection,
    ):
        yield connection


def test_maint_holds_exactly_its_allowlist(su: Connection) -> None:
    found = _privileges(su)
    assert sorted(found) == sorted(EXPECTED)  # no duplicate, nothing missing, nothing more


def test_maint_holds_nothing_in_another_database_and_owns_nothing(su: Connection) -> None:
    """``pg_shdepend`` lists every grant and ownership naming the role, in every database."""
    rows = su.execute(
        text(
            "SELECT s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "OR s.classid = CAST('pg_database' AS regclass), s.deptype FROM pg_shdepend s "
            "WHERE s.refobjid = CAST(:r AS regrole)"
        ),
        {"r": MAINT_ROLE},
    ).all()
    assert rows
    assert all(here for here, _ in rows)
    assert {kind for _, kind in rows} == {"a"}  # ACL entries only, never ownership


def test_maint_reads_no_secret_column_and_no_forbidden_table(su: Connection) -> None:
    for table in FORBIDDEN_TABLES:
        if su.execute(text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}).scalar():
            assert not su.execute(
                text("SELECT has_any_column_privilege(:r, :t, 'SELECT, INSERT, UPDATE')"),
                {"r": MAINT_ROLE, "t": f"public.{table}"},
            ).scalar(), table
            for privilege in ("SELECT", *WRITE_PRIVILEGES):
                assert not su.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"),
                    {"r": MAINT_ROLE, "t": f"public.{table}", "p": privilege},
                ).scalar(), (table, privilege)
    for table, columns in SECRET_COLUMNS.items():
        for column in columns:
            assert not su.execute(
                text("SELECT has_column_privilege(:r, :t, :c, 'SELECT')"),
                {"r": MAINT_ROLE, "t": f"public.{table}", "c": column},
            ).scalar(), f"{table}.{column}"


def test_the_allowlist_has_no_entry_on_a_forbidden_name() -> None:
    """The registry and the allowlist agree on paper too, so a careless edit of one is caught
    even before the database is consulted."""
    for obj, _ in EXPECTED:
        table, _, column = obj.partition(".")
        assert table not in FORBIDDEN_TABLES, obj
        assert column not in SECRET_COLUMNS.get(table, set()), obj


@pytest.mark.parametrize("role", [MAINT_ROLE, "ownvitals_app"])
def test_no_runtime_role_can_change_or_empty_the_audit_log(su: Connection, role: str) -> None:
    """Append-only by privileges (Architect decision, #12): no UPDATE, DELETE or TRUNCATE on
    ``admin_audit`` for maint or the app role, and the app role cannot even insert or read."""
    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
        assert not su.execute(
            text("SELECT has_table_privilege(:r, 'public.admin_audit', :p)"),
            {"r": role, "p": privilege},
        ).scalar()
    assert not su.execute(
        text("SELECT has_any_column_privilege(:r, 'public.admin_audit', 'UPDATE')"), {"r": role}
    ).scalar()
    if role == "ownvitals_app":
        assert not su.execute(
            text("SELECT has_any_column_privilege(:r, 'public.admin_audit', 'SELECT, INSERT')"),
            {"r": role},
        ).scalar()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE admin_audit SET reason = 'x'",
        "UPDATE admin_audit SET actor_email = 'x@example.com'",
        "DELETE FROM admin_audit",
        "TRUNCATE admin_audit",
    ],
)
def test_maint_is_refused_when_it_tries(admin_schema: str, statement: str) -> None:
    with engine_for(admin_schema) as engine, engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ WRITE"))
        with pytest.raises(DBAPIError, match="permission denied"):
            connection.execute(text(statement))


def test_maint_role_attributes_and_settings(su: Connection) -> None:
    role = su.execute(
        text(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
            "rolreplication, rolinherit, "
            "(SELECT count(*) FROM pg_auth_members m WHERE r.oid IN (m.member, m.roleid)), "
            "ARRAY(SELECT c FROM pg_db_role_setting s, unnest(s.setconfig) c "
            "WHERE s.setrole = r.oid ORDER BY 1) FROM pg_roles r WHERE rolname = :r"
        ),
        {"r": MAINT_ROLE},
    ).one()
    assert tuple(role[:8]) == (True, False, True, False, False, False, False, 0)
    assert list(role[8]) == [
        "default_transaction_read_only=on",
        "idle_in_transaction_session_timeout=60s",
        "lock_timeout=5s",
        "statement_timeout=30s",
        "transaction_timeout=5min",
    ]


def test_the_script_repairs_drift_when_run_again(su: Connection) -> None:
    """A grant too many, a membership, a changed setting and a lost attribute are all undone."""
    su.execute(text(f"GRANT SELECT ON public.users TO {MAINT_ROLE}"))
    su.execute(text(f"GRANT UPDATE ON public.admin_audit TO {MAINT_ROLE}"))
    su.execute(text(f"GRANT ownvitals_app TO {MAINT_ROLE}"))
    su.execute(text(f"ALTER ROLE {MAINT_ROLE} SET statement_timeout = 0"))
    su.execute(text(f"ALTER ROLE {MAINT_ROLE} NOBYPASSRLS INHERIT"))
    run_script(admin_url(), MAINT_SCRIPT.read_text(encoding="utf-8"))
    assert sorted(_privileges(su)) == sorted(EXPECTED)
    test_maint_role_attributes_and_settings(su)


def test_the_script_is_plain_sql_the_installer_accepts() -> None:
    """``ownvitals.db.install_functions`` (#126 wires it in) runs plain SQL in one transaction
    and refuses transaction control; the script passes as shipped, has no psql meta-command and
    sets no password (#126's)."""
    from ownvitals.db.install_functions import read_scripts

    source = read_scripts([str(MAINT_SCRIPT)])
    assert not [line for line in source.splitlines() if line.lstrip().startswith("\\")]
    assert not re.search(r"\bPASSWORD\s+['$:]", source, re.IGNORECASE)


def test_only_the_admin_database_module_builds_the_maint_engine() -> None:
    """The maint URL is read, and a session on it built, in ``ownvitals.admin.db`` only."""
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text("utf-8"))
        names = {
            n.attr if isinstance(n, ast.Attribute) else n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute | ast.Name)
        }
        if names & {"maint_sqlalchemy_url", "create_maint_engine", "_maint_factory"}:
            found.append(relative)
    assert found == ["admin/db.py"]  # config.py only defines maint_sqlalchemy_url
