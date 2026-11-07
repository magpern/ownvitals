"""The ``ownvitals_maint`` role check of the admin database path (ADR 0002 section 5): the role
web checks at startup and on every new connection, and the state a pooled connection must be in
at checkout. ``ownvitals.admin.db`` builds the engine that runs these checks and re-exports them.
"""

from typing import Any, Final

from sqlalchemy.exc import DisconnectionError

from ownvitals.db.role_check import EXPECTED_ROLE_SETTINGS

MAINT_ROLE: Final = "ownvitals_maint"
# ADR 0002 section 5 (#78): the 5-minute ``transaction_timeout`` bound, as 30-maint.sql sets it.
TRANSACTION_TIMEOUT: Final = "5min"
EXPECTED_MAINT_SETTINGS: Final = {
    **EXPECTED_ROLE_SETTINGS,
    "transaction_timeout": TRANSACTION_TIMEOUT,
    "default_transaction_read_only": "on",
}

ROLE_QUERY: Final = """
SELECT current_user::text, r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole,
       r.rolreplication, r.rolinherit, r.rolcanlogin,
       (SELECT count(*) FROM pg_catalog.pg_auth_members m WHERE r.oid IN (m.member, m.roleid)),
       ARRAY(SELECT s.setdatabase::text || ':' || c FROM pg_catalog.pg_db_role_setting s,
                    unnest(s.setconfig) c WHERE s.setrole = r.oid ORDER BY 1),
       EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relowner = r.oid)
         OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspowner = r.oid)
         OR EXISTS (SELECT 1 FROM pg_catalog.pg_database d WHERE d.datdba = r.oid)
         OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.proowner = r.oid)
  FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
"""
CHECKOUT_QUERY: Final = """
SELECT current_setting('default_transaction_read_only'), current_setting('transaction_read_only'),
       ARRAY(SELECT name || '=' || setting FROM pg_catalog.pg_settings WHERE name IN
             ('statement_timeout', 'lock_timeout', 'idle_in_transaction_session_timeout',
              'transaction_timeout')
             ORDER BY 1)
"""
EXPECTED_TIMEOUTS_MS: Final = [
    "idle_in_transaction_session_timeout=60000",
    "lock_timeout=5000",
    "statement_timeout=30000",
    "transaction_timeout=300000",
]


class MaintRoleError(RuntimeError):
    """The admin database role is not exactly ``ownvitals_maint`` as ADR 0002 section 5 sets
    it. Never contains credentials."""


def role_problems(row: Any) -> list[str]:
    """Problems for one ``ROLE_QUERY`` row; empty when the role is exactly ownvitals_maint."""
    if row is None:
        return ["the connected role does not exist in pg_roles"]
    (user, superuser, bypass, createdb, createrole, replication, inherit, login, members) = row[:9]
    settings, owns = row[9], row[10]
    found: list[str] = []
    if user != MAINT_ROLE:
        found.append(f"connected as another role than {MAINT_ROLE}")
    if superuser:
        found.append("is a superuser")
    if not bypass:
        found.append("lacks BYPASSRLS")
    if createdb or createrole or replication:
        found.append("may create databases or roles or replicate")
    if inherit:
        found.append("inherits")
    if not login:
        found.append("cannot log in")
    if members:
        found.append("is a member of a role or has members")
    expected = sorted(f"0:{k}={v}" for k, v in EXPECTED_MAINT_SETTINGS.items())
    if sorted(settings or []) != expected:
        found.append("has role settings other than the four timeouts and the read-only default")
    if owns:
        found.append("owns a relation, schema, database or function")
    return found


def _raise_if_wrong(row: Any) -> None:
    found = role_problems(row)
    if found:
        raise MaintRoleError(
            "the admin database role " + "; it ".join(found) + " (ADR 0002 section 5)"
        )


def check_checkout(dbapi_connection: Any) -> None:
    """A pooled maint connection as the role defaults leave it, or ``DisconnectionError`` (the
    pool then opens a fresh one). Runs in its own transaction and rolls it back."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(CHECKOUT_QUERY)
        default_ro, current_ro, timeouts = cursor.fetchone()
    finally:
        cursor.close()
    dbapi_connection.rollback()
    if default_ro != "on" or current_ro != "on" or list(timeouts) != EXPECTED_TIMEOUTS_MS:
        raise DisconnectionError("a pooled admin connection carries state from an earlier use")


def _check_connection(dbapi_connection: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(ROLE_QUERY)
        row = cursor.fetchone()
    finally:
        cursor.close()
    dbapi_connection.rollback()
    _raise_if_wrong(row)
