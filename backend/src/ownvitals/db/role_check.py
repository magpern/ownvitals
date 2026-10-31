"""Startup refusal (ADR 0002 section 5, guard 6): web and worker must not run as a role that
sidesteps RLS.

The check runs once at startup (``verify_database_role``) and again on every new pooled
connection (``refuse_privileged_roles``), so a database that was down at startup cannot let a
privileged connection through later. There is no override, in any environment.
"""

import logging
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import DisconnectionError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# One row of booleans for the connected role. Memberships are followed transitively, because a
# member of a BYPASSRLS or superuser role can SET ROLE to it.
ROLE_QUERY = """
WITH RECURSIVE me AS (
    SELECT oid FROM pg_roles WHERE rolname = current_user
), reachable AS (
    SELECT oid FROM me
    UNION
    SELECT am.roleid FROM pg_auth_members am JOIN reachable r ON am.member = r.oid
)
SELECT
    EXISTS (SELECT 1 FROM pg_roles x WHERE x.oid = (SELECT oid FROM me) AND x.rolsuper),
    EXISTS (SELECT 1 FROM pg_roles x WHERE x.oid = (SELECT oid FROM me) AND x.rolbypassrls),
    EXISTS (SELECT 1 FROM pg_roles x JOIN reachable r ON r.oid = x.oid
             WHERE x.oid <> (SELECT oid FROM me) AND (x.rolsuper OR x.rolbypassrls)),
    EXISTS (SELECT 1 FROM pg_class c WHERE c.relowner = (SELECT oid FROM me)
             AND c.relkind IN ('r', 'p', 'f', 'm', 'v')),
    EXISTS (SELECT 1 FROM pg_database d WHERE d.datname = current_database()
             AND d.datdba = (SELECT oid FROM me)),
    EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspowner = (SELECT oid FROM me)),
    EXISTS (SELECT 1 FROM pg_roles x JOIN reachable r ON r.oid = x.oid
             WHERE x.rolname IN ('pg_execute_server_program', 'pg_read_server_files',
                                 'pg_write_server_files')),
    ARRAY(SELECT s.setdatabase::text || ':' || c FROM pg_db_role_setting s, unnest(s.setconfig) c
           WHERE s.setrole = (SELECT oid FROM me))
"""
# The role's own settings must be exactly these, at role level (database 0). A role can change
# its own settings (ALTER ROLE ... SET) and Postgres cannot forbid it, so this detects it.
EXPECTED_ROLE_SETTINGS = {
    "statement_timeout": "30s",
    "lock_timeout": "5s",
    "idle_in_transaction_session_timeout": "60s",
}
SETTINGS_PROBLEM = "has role settings other than the expected three timeouts"
FLAG_COUNT = 7
CHECKOUT_QUERY = "SELECT current_setting('app.user_id', true)"
SETTINGS_MS_QUERY = (
    "SELECT name, setting FROM pg_settings WHERE name IN "
    "('statement_timeout', 'lock_timeout', 'idle_in_transaction_session_timeout')"
)
EXPECTED_TIMEOUTS_MS = {
    "statement_timeout": "30000",
    "lock_timeout": "5000",
    "idle_in_transaction_session_timeout": "60000",
}
PROBLEMS = (
    "is a superuser",
    "has BYPASSRLS",
    "is a member of a superuser or BYPASSRLS role",
    "owns tables, views or materialized views",
    "owns the database",
    "owns a schema",
    "is a member of pg_execute_server_program, pg_read_server_files or pg_write_server_files",
)


class DatabaseRoleError(RuntimeError):
    """The configured database role could bypass row-level security. Never contains credentials."""


def problems_for(row: Any) -> list[str]:
    """Problems for one ``ROLE_QUERY`` row: the seven flags, then the role's settings."""
    if row is None or len(row) != FLAG_COUNT + 1:
        raise ValueError("the role query must return the seven flags and the settings")
    found = [problem for problem, flag in zip(PROBLEMS, row[:FLAG_COUNT], strict=True) if flag]
    expected = {f"0:{key}={value}" for key, value in EXPECTED_ROLE_SETTINGS.items()}
    if set(row[FLAG_COUNT]) != expected:
        found.append(SETTINGS_PROBLEM)
    return found


def _raise_if_unsafe(row: Any) -> None:
    found = problems_for(row)
    if found:
        raise DatabaseRoleError(
            "the database role used by web and worker "
            + "; it ".join(found)
            + " (ADR 0002 section 5)"
        )


def refuse_privileged_roles(engine: AsyncEngine) -> None:
    """Check every new connection of ``engine``; a privileged role is refused with an error.

    Every checkout also checks that the pooled connection carries no ``app.user_id`` and has
    exactly the role's timeouts; otherwise the pool replaces it.
    """

    @event.listens_for(engine.sync_engine, "checkout")
    def check_checkout(dbapi_connection: Any, _record: object, _proxy: object) -> None:
        # A pooled connection must come back exactly as the role defaults leave it: no
        # session-level app.user_id left by a committed SET, and the role's timeouts. Otherwise
        # it is invalidated and the pool opens a fresh one. The ROLLBACK at the end also makes
        # psycopg forget the statements it prepared (see engine.discard_session_state).
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(CHECKOUT_QUERY)
            (user_id,) = cursor.fetchone()
            cursor.execute(SETTINGS_MS_QUERY)
            actual = dict(cursor.fetchall())
        finally:
            cursor.close()
        dbapi_connection.rollback()
        if user_id not in (None, "") or actual != EXPECTED_TIMEOUTS_MS:
            raise DisconnectionError("a pooled connection carries state from an earlier use")

    @event.listens_for(engine.sync_engine, "connect")
    def check_connection(dbapi_connection: Any, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(ROLE_QUERY)
            row = cursor.fetchone()
        finally:
            cursor.close()
        dbapi_connection.rollback()
        _raise_if_unsafe(row)


def is_authentication_failure(exc: OperationalError) -> bool:
    """True when the server answered and rejected the login (not when it was unreachable)."""
    text_ = str(exc.orig).lower()
    return any(
        marker in text_
        for marker in ("authentication failed", "no password supplied", "password required")
    ) or (getattr(exc.orig, "sqlstate", None) or "").startswith("28")


async def verify_database_role(engine: AsyncEngine) -> None:
    """Refuse to start when the role is privileged (ADR 0002 section 5).

    An unreachable database is not an error here: the per-connection check still applies to every
    connection made later."""
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(ROLE_QUERY))).one()
    except OperationalError as exc:
        if is_authentication_failure(exc):
            # Fatal: a wrong password or role is a configuration error, not a database that is
            # down. The message names no user, host or password.
            raise DatabaseRoleError(
                "the database refused the configured credentials; check database_url and the "
                "database_password secret file"
            ) from None
        logger.debug("database unreachable at startup; role checked on first connection")
        return
    _raise_if_unsafe(row)
