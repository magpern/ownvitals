"""Startup refusal (ADR 0002 section 5, guard 6): web and worker must not run as a role that
sidesteps RLS.

The check runs once at startup (``verify_database_role``) and again on every new pooled
connection (``refuse_privileged_roles``), so a database that was down at startup cannot let a
privileged connection through later. There is no override, in any environment.
"""

import logging
import re
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.exc import DisconnectionError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# One row for the connection: the flags of ``PROBLEMS``, then the login role's own settings.
#
# ``me`` is the **login** role (``session_user``, the user of the URL), not the role the session
# runs as (``current_user``). The two differ when the login role has a default ``role`` setting
# (ALTER ROLE ... SET role) or the connection options set one, and a login role can always go
# back to itself (``SET ROLE NONE``) or on to any role it may SET: a superuser login, or a member
# of ``ownvitals_owner``, that starts as ``ownvitals_app`` bypasses RLS whenever it likes. So the
# checks run on the login role, and flag 9 refuses a ``current_user`` other than the login role.
# Every role reachable from ``current_user`` is reachable from the login role too (Postgres only
# lets a login become a role it is a member of, or any role for a superuser).
#
# ``roles`` is the login role and every role it is a member of, directly or indirectly, with or
# without INHERIT or SET (``pg_has_role(..., 'MEMBER')`` ignores both options). A member of a role
# can use that role's rights: through INHERIT, or by SET ROLE. A member of the role that owns the
# tables can then disable their row security, and a member of ``pg_read_all_data`` ignores every
# grant. So **any** membership is refused (flag 4; ADR 0002 section 5: "member of no role"), and
# the attribute and ownership flags are evaluated over ``roles`` as well, so each report names
# what the membership would give. No membership is allowed: the roles script grants none.
#
# Flag 8 refuses a default for any ``app.*`` setting (``app.user_id`` is the RLS user) on the
# login role (any database), on the database (``ALTER DATABASE ... SET``) or on every role
# (``ALTER ROLE ALL [IN DATABASE ...] SET``): such a default would start every session as a user.
# Postgres keeps the name as written and compares it case-insensitively, hence ``lower``.
# Flag 10 reads the session's live ``app.user_id``, which a server-wide setting (``ALTER
# SYSTEM``, ``postgresql.conf``) or the connection options (``PGOPTIONS``, ``options=-c ...``)
# set without any catalog row, so a new connection is refused with a ``DatabaseRoleError``
# rather than replaced by the checkout check over and over.
ROLE_QUERY = """
WITH me AS (
    SELECT oid FROM pg_roles WHERE rolname = session_user
), roles AS (
    SELECT r.oid, r.rolsuper, r.rolbypassrls, r.rolreplication FROM pg_roles r
    WHERE r.oid = (SELECT oid FROM me) OR pg_has_role((SELECT oid FROM me), r.oid, 'MEMBER')
)
SELECT
    EXISTS (SELECT 1 FROM roles WHERE rolsuper),
    EXISTS (SELECT 1 FROM roles WHERE rolbypassrls),
    EXISTS (SELECT 1 FROM roles WHERE rolreplication),
    EXISTS (SELECT 1 FROM roles WHERE oid <> (SELECT oid FROM me)),
    EXISTS (SELECT 1 FROM pg_class c JOIN roles r ON r.oid = c.relowner
             WHERE c.relkind IN ('r', 'p', 'f', 'm', 'v')),
    EXISTS (SELECT 1 FROM pg_database d JOIN roles r ON r.oid = d.datdba
             WHERE d.datname = current_database()),
    EXISTS (SELECT 1 FROM pg_namespace n JOIN roles r ON r.oid = n.nspowner),
    EXISTS (SELECT 1 FROM pg_db_role_setting s, unnest(s.setconfig) c
             WHERE (s.setrole = (SELECT oid FROM me)
                    OR (s.setrole = 0 AND s.setdatabase IN
                        (0, (SELECT oid FROM pg_database WHERE datname = current_database()))))
               AND starts_with(lower(split_part(c, '=', 1)), 'app.')),
    session_user <> current_user,
    coalesce(current_setting('app.user_id', true), '') <> '',
    ARRAY(SELECT s.setdatabase::text || ':' || c FROM pg_db_role_setting s, unnest(s.setconfig) c
           WHERE s.setrole = (SELECT oid FROM me))
"""
# The login role's own settings must be exactly these, at role level (database 0). A role can
# change its own settings (ALTER ROLE ... SET) and Postgres cannot forbid it, so this detects it.
# Only the login role's settings apply to a session (SET ROLE does not load the other role's).
EXPECTED_ROLE_SETTINGS = {
    "statement_timeout": "30s",
    "lock_timeout": "5s",
    "idle_in_transaction_session_timeout": "60s",
}
SETTINGS_PROBLEM = "has role settings other than the expected three timeouts"
FLAG_COUNT = 10
INSUFFICIENT_PRIVILEGE = "42501"
INVALID_CATALOG_NAME = "3D000"  # the database does not exist
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
    "is a superuser or a member of one",
    "has BYPASSRLS or is a member of a role that has it",
    "has REPLICATION or is a member of a role that has it",
    "is a member of another role (no membership is allowed)",
    "owns tables, views or materialized views, or is a member of a role that does",
    "owns the database, or is a member of its owner",
    "owns a schema, or is a member of a role that does",
    "has a default for an app.* setting (on the role, the database or every role)",
    "logs in as one role and runs as another (a default or connection option for role)",
    "starts each session with app.user_id set (server configuration or connection options)",
)


class DatabaseRoleError(RuntimeError):
    """The configured database role could bypass row-level security. Never contains credentials."""


def problems_for(row: Any) -> list[str]:
    """Problems for one ``ROLE_QUERY`` row: the ``FLAG_COUNT`` flags, then the role's settings."""
    if row is None or len(row) != FLAG_COUNT + 1:
        raise ValueError(f"the role query must return the {FLAG_COUNT} flags and the settings")
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


# A login the server answered and refused: SQLSTATE class 28 (invalid authorization), 42501
# (insufficient privilege) or 3D000 (the database does not exist). libpq hands psycopg no
# SQLSTATE for an error at connect, so the server's message is matched too: a wrong password or
# role, a role that may not log in, no ``pg_hba.conf`` entry, no CONNECT privilege, a connection
# option the role may not set (``-c session_authorization=...`` or ``-c role=...`` in
# ``options`` or ``PGOPTIONS``), or a database name that does not exist. The server's own
# refusals are matched only after its ``FATAL:``, so a client-side "permission denied" (a
# socket) stays a connection failure. The messages are the server's English ones (``lc_messages``
# ``en_US`` or ``C``, the Postgres default; docs/dev/backend.md). Anything else at connect
# (refused, a timeout, DNS, a server that is starting up or full) is a database that is
# unreachable for now; a ``FATAL:`` answer among those is logged as a warning.
REFUSED_LOGIN = re.compile(
    r"authentication failed|no password supplied|password required"
    r'|fatal:\s+(?:permission denied|role "[^"]*" (?:does not exist|is not permitted to log in)'
    r'|database "[^"]*" does not exist|no pg_hba\.conf entry|pg_hba\.conf rejects connection)'
)
REFUSED_SQLSTATES = {INSUFFICIENT_PRIVILEGE, INVALID_CATALOG_NAME}


def is_authentication_failure(exc: OperationalError) -> bool:
    """True when the server answered and refused the login (``REFUSED_LOGIN``), not when it was
    unreachable."""
    sqlstate = getattr(exc.orig, "sqlstate", None) or ""
    return (
        sqlstate.startswith("28")
        or sqlstate in REFUSED_SQLSTATES
        or REFUSED_LOGIN.search(str(exc.orig).lower()) is not None
    )


def log_deferral(exc: OperationalError, what: str) -> None:
    """Log a check deferred because the database was unreachable at startup. A ``FATAL:`` answer
    from the server that ``is_authentication_failure`` doesn't recognise is a warning: it may be
    a server that is starting up or full, or a refusal in a language other than English. The
    line holds the SQLSTATE class when there is one and never the message text (it can name the
    user, host or database)."""
    if "fatal:" in str(exc.orig).lower():
        sqlstate = getattr(exc.orig, "sqlstate", None) or ""
        logger.warning(
            "the database answered FATAL at startup (SQLSTATE class %s), not recognised as a "
            "refused login; %s checked on first connection (the server must use English "
            "messages: lc_messages en_US or C)",
            sqlstate[:2] or "unknown",
            what,
        )
    else:
        logger.debug("database unreachable at startup; %s checked on first connection", what)


async def verify_database_role(engine: AsyncEngine) -> None:
    """Refuse to start when the role is privileged (ADR 0002 section 5), or when the database
    refuses its login (``is_authentication_failure``): both raise ``DatabaseRoleError``.

    An unreachable database is not an error here: the per-connection check still applies to every
    connection made later."""
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(ROLE_QUERY))).one()
    except OperationalError as exc:
        if is_authentication_failure(exc):
            # Fatal: a wrong password or role, or a login the role may not make, is a
            # configuration error, not a database that is down. The message names no user, host
            # or password.
            raise DatabaseRoleError(
                "the database refused the configured credentials or login (a wrong password, "
                "role or database name, no CONNECT privilege, or a connection option the role may "
                "not set); check database_url, PGOPTIONS and the database_password secret file"
            ) from None
        log_deferral(exc, "role")
        return
    _raise_if_unsafe(row)
