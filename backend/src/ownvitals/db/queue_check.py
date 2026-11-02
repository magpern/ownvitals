"""The queue check of ADR 0005 section 3 (guard 9): the worker refuses to run on a drifted queue.

The ``SECURITY DEFINER`` functions of ``docker/postgres/queue/20-queue.sql`` and their owner
``ownvitals_queue`` are the only cross-user paths into the owned job tables. The check runs at
startup (``verify_queue``) and on every new pooled connection (``install_queue_check``), like the
role check, and refuses on any difference with ``QueueDriftError`` (a ``DatabaseRoleError``; no
override): each function exists once, owned by ``ownvitals_queue``, ``SECURITY DEFINER`` with
exactly the fixed ``search_path``, the version comment, the shipped ``md5(pg_get_functiondef)``
(``ownvitals.db.queue_digests``) and the ACL ``{ownvitals_queue=X/ownvitals_queue,
ownvitals_app=X/ownvitals_queue}``; the role has exactly its attributes, no memberships either way,
no settings, no default privileges and exactly its privileges; no other user-defined function,
procedure, aggregate or operator exists in any schema, except the three Withings functions
``ensure_month_partitions``, ``webhook_secrets_apply`` and ``webhook_event_insert`` admitted by
name in ``20-queue.sql``, and the rate functions ``withings_rate_claim``,
``withings_rate_release`` and ``withings_rate_pause`` admitted the same way, each pinned like a
queue function when it is installed;
no table has a trigger (other than the internal ones of foreign keys) or a rule; and no default,
CHECK, index or partition expression of any table or domain calls one of ``SQL_TEXT_BUILTINS``.
So nothing can shadow a built-in a function body calls, and nothing a function fires (a trigger,
a rule, a default, a CHECK, an index expression) can reach code other than the built-ins while
running as ``ownvitals_queue``, and none of those runs SQL text. Objects are named alike here,
in guard 9 (``tests/queue_guard.py``) and in ``20-queue.sql`` (``function schema.name(types)``,
``trigger name on schema.table``, ...).

Every grant or ownership naming a role is recorded in ``pg_shdepend``, so comparing that list with
the expected one catches a privilege of any kind, in any database of the cluster.

Where it runs (#9): the worker (``ownvitals.worker``) and ``python -m ownvitals jobs stats``. The
web process does not refuse on a drifted queue yet: it calls no queue function, and its startup
check comes with the test harness change of #77 (item 3), a recorded gap against ADR 0005 section
3.
"""

import logging
import weakref
from collections.abc import Sequence
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.db.queue_digests import QUEUE_DIGESTS
from ownvitals.db.role_check import DatabaseRoleError, is_authentication_failure

logger = logging.getLogger(__name__)

QUEUE_ROLE = "ownvitals_queue"
QUEUE_VERSION = "ownvitals-queue v2"
SEARCH_PATH = "search_path=pg_catalog, pg_temp"
# name -> identity arguments, as pg_get_function_identity_arguments renders them.
QUEUE_FUNCTIONS: dict[str, str] = {
    "queue_claim": "p_worker text, p_lease_seconds integer, p_kinds text[]",
    "queue_reap": "p_limit integer",
    "queue_stats": "",
    "queue_enqueue": (
        "p_connection_id uuid, p_kind text, p_origin text, p_input jsonb, "
        "p_singleton_key text, p_max_attempts integer, p_target_secret_id uuid"
    ),
    "queue_cancel_for_secret": "p_kind text, p_secret_id uuid",
    "queue_replay": "p_job_id uuid",
}
# Admitted beside the queue (20-queue.sql). A queue-only database does not have them; a deploy
# installs them and the check then pins each body the same way as a queue function.
WITHINGS_FUNCTION_NAMES = (
    "ensure_month_partitions",
    "webhook_secrets_apply",
    "webhook_event_insert",
)
WITHINGS_VERSION = "ownvitals-withings v1"
# name -> (identity arguments, owner, proconfig joined by ';', ACL joined by ',')
WITHINGS_PINS: dict[str, tuple[str, str, str, str]] = {
    "ensure_month_partitions": (
        "p_table text, p_from date, p_to date",
        "ownvitals_owner",
        "search_path=pg_catalog, pg_temp;TimeZone=UTC",
        "ownvitals_app=X/ownvitals_owner,ownvitals_owner=X/ownvitals_owner",
    ),
    "webhook_secrets_apply": (
        "p_app_id integer, p_operation text, p_secret_id text, p_seed_id text, "
        "p_pepper_id text, p_hmac bytea, p_active_from timestamp with time zone, "
        "p_seen_requested_at timestamp with time zone, p_seen_reason text",
        "ownvitals_webhook_secrets",
        "search_path=pg_catalog, pg_temp",
        "ownvitals_app=X/ownvitals_webhook_secrets,"
        "ownvitals_webhook_secrets=X/ownvitals_webhook_secrets",
    ),
    "webhook_event_insert": (
        "p_app_id integer, p_withings_userid bigint, p_appli integer, p_startdate bigint, "
        "p_enddate bigint, p_dedupe_key text, p_job_id uuid",
        "ownvitals_webhook_events",
        "search_path=pg_catalog, pg_temp",
        "ownvitals_app=X/ownvitals_webhook_events,ownvitals_webhook_events=X/ownvitals_webhook_events",
    ),
}
# Admitted with the Withings functions (20-queue.sql, rate.sql). Absent until the migration
# and rate.sql have run; once present, pinned the same way.
RATE_FUNCTION_NAMES = (
    "withings_rate_claim",
    "withings_rate_release",
    "withings_rate_pause",
)
RATE_VERSION = "ownvitals-rate v1"
_RATE_CONFIG = "search_path=pg_catalog, pg_temp;TimeZone=UTC"
_RATE_ACL = "ownvitals_app=X/ownvitals_owner,ownvitals_owner=X/ownvitals_owner"
RATE_PINS: dict[str, tuple[str, str, str, str]] = {
    "withings_rate_claim": (
        "p_app_id integer, p_account_id uuid, p_lane integer, p_lease_seconds integer",
        "ownvitals_owner",
        _RATE_CONFIG,
        _RATE_ACL,
    ),
    "withings_rate_release": ("p_lease_id uuid", "ownvitals_owner", _RATE_CONFIG, _RATE_ACL),
    "withings_rate_pause": (
        "p_app_id integer, p_wait_seconds double precision",
        "ownvitals_owner",
        _RATE_CONFIG,
        _RATE_ACL,
    ),
}
EXPECTED_ACL = "ownvitals_app=X/ownvitals_queue,ownvitals_queue=X/ownvitals_queue"
# 40-withings.sql grants the ingest role EXECUTE on queue_enqueue so the notification and the
# job commit together. 20-queue.sql revokes that grant when it is re-run on its own, so both
# ACLs are the shipped ones: with the ingest role, and without it.
ENQUEUE_INGEST_ACL = EXPECTED_ACL + ",ownvitals_webhook_events=X/ownvitals_queue"
# rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit
EXPECTED_ATTRIBUTES = [False, False, False, False, False, True, False]
# What ownvitals_queue may hold: ACL entries ("a") and ownership ("o"), as pg_shdepend lists them.
EXPECTED_DEPENDENCIES = frozenset(
    {f"pg_proc:public.{name}({args}):0:o" for name, args in QUEUE_FUNCTIONS.items()}
    | {
        "pg_class:public.jobs:0:a",
        "pg_class:public.job_events:0:a",
        "pg_class:public.connections:1:a",  # id
        "pg_class:public.connections:2:a",  # owner_id
        "pg_class:public.connections:4:a",  # status
        "pg_namespace:public:0:a",
    }
)
EXPECTED_PRIVILEGES = frozenset(
    {
        "schema:public:USAGE",
        "table:public.jobs:INSERT",
        "table:public.jobs:SELECT",
        "table:public.jobs:UPDATE",
        "table:public.job_events:SELECT",
        "table:public.job_events:INSERT",
    }
)

# Built-ins that run SQL text or read whole tables, schemas or databases (``query_to_xml`` and its
# family, ``ts_stat``, ``ts_rewrite(tsquery, text)``) or change the running function's settings
# (``set_config``, e.g. its search_path): reachable from a default, CHECK, index or partition
# expression on a table a queue function writes, they would run read-only SQL as ownvitals_queue.
# No expression in any table, domain or index may call one; found by the OIDs in the stored
# expression trees (``:funcid``), not by text.
SQL_TEXT_BUILTINS = (
    "query_to_xml",
    "query_to_xmlschema",
    "query_to_xml_and_xmlschema",
    "cursor_to_xml",
    "cursor_to_xmlschema",
    "table_to_xml",
    "table_to_xmlschema",
    "table_to_xml_and_xmlschema",
    "schema_to_xml",
    "schema_to_xmlschema",
    "schema_to_xml_and_xmlschema",
    "database_to_xml",
    "database_to_xmlschema",
    "database_to_xml_and_xmlschema",
    "ts_stat",
    "ts_rewrite",
    "set_config",
)
_NAMES = ", ".join(
    f"'{name}'" for name in (*QUEUE_FUNCTIONS, *WITHINGS_FUNCTION_NAMES, *RATE_FUNCTION_NAMES)
)

# 16384 is FirstNormalObjectId: below it is only what initdb created (pg_catalog,
# information_schema and the two rules of the pg_settings view). Anything at or above it, in any
# schema, is user-made.
QUEUE_QUERY = """
WITH q AS (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_queue')
SELECT
    (SELECT ARRAY[r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication,
                  r.rolbypassrls, r.rolinherit]
       FROM pg_catalog.pg_roles r WHERE r.oid = (SELECT oid FROM q)),
    (SELECT count(*) FROM pg_catalog.pg_auth_members m
      WHERE (SELECT oid FROM q) IN (m.member, m.roleid)),
    (SELECT count(*) FROM pg_catalog.pg_db_role_setting s WHERE s.setrole = (SELECT oid FROM q))
      + (SELECT count(*) FROM pg_catalog.pg_default_acl d
          WHERE d.defaclrole = (SELECT oid FROM q)),
    ARRAY(
        SELECT s.classid::regclass::text || ':' ||
               CASE WHEN s.dbid NOT IN (0, (SELECT oid FROM pg_catalog.pg_database
                                             WHERE datname = current_database()))
                      THEN 'another database ' || s.objid::text
                    WHEN s.classid = 'pg_catalog.pg_class'::regclass
                      THEN (SELECT n.nspname || '.' || c.relname FROM pg_catalog.pg_class c
                              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                             WHERE c.oid = s.objid)
                    WHEN s.classid = 'pg_catalog.pg_namespace'::regclass
                      THEN (SELECT n.nspname FROM pg_catalog.pg_namespace n WHERE n.oid = s.objid)
                    WHEN s.classid = 'pg_catalog.pg_proc'::regclass
                      THEN (SELECT n.nspname || '.' || p.proname || '('
                                   || pg_catalog.pg_get_function_identity_arguments(p.oid) || ')'
                              FROM pg_catalog.pg_proc p
                              JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                             WHERE p.oid = s.objid)
                    ELSE s.objid::text END
               || ':' || s.objsubid::text || ':' || s.deptype::text
          FROM pg_catalog.pg_shdepend s
         WHERE s.refclassid = 'pg_catalog.pg_authid'::regclass AND s.refobjid = (SELECT oid FROM q)
         ORDER BY 1),
    ARRAY(
        SELECT 'schema:' || n.nspname || ':' || a.privilege_type
               || CASE WHEN a.is_grantable THEN ' with grant option' ELSE '' END
          FROM pg_catalog.pg_namespace n, aclexplode(n.nspacl) a
         WHERE a.grantee = (SELECT oid FROM q)
        UNION ALL
        SELECT 'table:' || n.nspname || '.' || c.relname || ':' || a.privilege_type
               || CASE WHEN a.is_grantable THEN ' with grant option' ELSE '' END
          FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
               aclexplode(c.relacl) a
         WHERE a.grantee = (SELECT oid FROM q)
        ORDER BY 1),
    ARRAY(
        SELECT p.proname || '|' || pg_catalog.pg_get_function_identity_arguments(p.oid)
               || '|' || p.prosecdef::text || '|' || pg_catalog.pg_get_userbyid(p.proowner)
               || '|' || coalesce(array_to_string(p.proconfig, ';'), '')
               || '|' || coalesce(pg_catalog.obj_description(p.oid, 'pg_proc'), '')
               || '|' || md5(pg_catalog.pg_get_functiondef(p.oid))
               || '|' || coalesce(array_to_string(
                             ARRAY(SELECT x::text FROM unnest(p.proacl) x ORDER BY 1), ','), '')
          FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND p.proname IN (__NAMES__)
         ORDER BY 1),
    ARRAY(
        SELECT 'function ' || n.nspname || '.' || p.proname || '('
               || pg_catalog.oidvectortypes(p.proargtypes) || ')'
          FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
         WHERE NOT (n.nspname = 'public' AND p.proname IN (__NAMES__))
           AND (p.oid >= 16384 OR n.nspname = 'public'
                OR (n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                    AND n.nspname !~ '^pg_(toast_)?temp_[0-9]+$'))
        UNION ALL
        SELECT 'operator ' || n.nspname || '.' || o.oprname || '('
               || CASE WHEN o.oprleft = 0 THEN 'NONE' ELSE o.oprleft::regtype::text END || ', '
               || CASE WHEN o.oprright = 0 THEN 'NONE' ELSE o.oprright::regtype::text END || ')'
          FROM pg_catalog.pg_operator o JOIN pg_catalog.pg_namespace n ON n.oid = o.oprnamespace
         WHERE o.oid >= 16384 OR n.nspname = 'public'
            OR (n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                AND n.nspname !~ '^pg_(toast_)?temp_[0-9]+$')
        UNION ALL
        SELECT 'trigger ' || t.tgname || ' on ' || n.nspname || '.' || c.relname
          FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE NOT t.tgisinternal
        UNION ALL
        SELECT 'rule ' || r.rulename || ' on ' || n.nspname || '.' || c.relname
          FROM pg_catalog.pg_rewrite r JOIN pg_catalog.pg_class c ON c.oid = r.ev_class
          JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
         WHERE r.oid >= 16384 AND r.rulename <> '_RETURN'
        UNION ALL
        SELECT DISTINCT 'built-in ' || f.proname || ' in ' || e.what
          FROM (
            SELECT 'the default of ' || n.nspname || '.' || c.relname || '.' || a.attname AS what,
                   d.adbin::text AS tree
              FROM pg_catalog.pg_attrdef d JOIN pg_catalog.pg_class c ON c.oid = d.adrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_catalog.pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
            UNION ALL
            SELECT 'constraint ' || k.conname || ' on ' || n.nspname || '.'
                   || coalesce(c.relname, y.typname), k.conbin::text
              FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_namespace n
                ON n.oid = k.connamespace
              LEFT JOIN pg_catalog.pg_class c ON c.oid = k.conrelid
              LEFT JOIN pg_catalog.pg_type y ON y.oid = k.contypid
             WHERE k.conbin IS NOT NULL
            UNION ALL
            SELECT 'index ' || n.nspname || '.' || c.relname,
                   coalesce(i.indexprs::text, '') || coalesce(i.indpred::text, '')
              FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid = i.indexrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE i.indexprs IS NOT NULL OR i.indpred IS NOT NULL
            UNION ALL
            SELECT 'the default of type ' || n.nspname || '.' || y.typname, y.typdefaultbin::text
              FROM pg_catalog.pg_type y JOIN pg_catalog.pg_namespace n ON n.oid = y.typnamespace
             WHERE y.typdefaultbin IS NOT NULL
            UNION ALL
            SELECT 'the partition key of ' || n.nspname || '.' || c.relname, x.partexprs::text
              FROM pg_catalog.pg_partitioned_table x JOIN pg_catalog.pg_class c
                ON c.oid = x.partrelid
              JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
             WHERE x.partexprs IS NOT NULL
          ) e
          CROSS JOIN LATERAL pg_catalog.regexp_matches(e.tree, '[:]funcid ([0-9]+)', 'g') m
          JOIN pg_catalog.pg_proc f ON f.oid = m[1]::oid
         WHERE f.pronamespace = 'pg_catalog'::regnamespace
           AND f.proname IN (__SQL_TEXT_BUILTINS__)
        ORDER BY 1)
""".replace("__SQL_TEXT_BUILTINS__", ", ".join(f"'{name}'" for name in SQL_TEXT_BUILTINS)).replace(
    "__NAMES__", _NAMES
)
COLUMN_COUNT = 7


class QueueDriftError(DatabaseRoleError):
    """The job queue does not match ADR 0005 section 3. Names objects, never data or credentials."""


def queue_problems(row: Sequence[Any] | None) -> list[str]:
    """Problems for one ``QUEUE_QUERY`` row; empty when the queue matches ADR 0005 section 3."""
    if row is None or len(row) != COLUMN_COUNT:
        raise ValueError(f"the queue query must return {COLUMN_COUNT} columns")
    attributes, memberships, settings, dependencies, privileges, functions, foreign = row
    if attributes is None:
        return [f"the role {QUEUE_ROLE} does not exist (run docker/postgres/queue/20-queue.sql)"]
    found: list[str] = []
    if list(attributes) != EXPECTED_ATTRIBUTES:
        found.append(
            f"{QUEUE_ROLE} must be NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "BYPASSRLS NOINHERIT"
        )
    if memberships:
        found.append(f"{QUEUE_ROLE} must be a member of no role and have no members")
    if settings:
        found.append(f"{QUEUE_ROLE} must have no role settings and no default privileges")
    if set(dependencies) != EXPECTED_DEPENDENCIES or len(dependencies) != len(set(dependencies)):
        found.append(f"{QUEUE_ROLE} owns or holds privileges other than the expected ones")
    if set(privileges) != EXPECTED_PRIVILEGES or len(privileges) != len(set(privileges)):
        found.append(f"{QUEUE_ROLE} must have exactly its table privileges and USAGE on public")
    if foreign:
        found.append(
            "user-defined functions, operators, triggers or rules, or expressions calling a "
            "built-in that runs SQL text, exist besides the queue functions (ADR 0005 section 3): "
            + ", ".join(foreign)
        )
    by_name: dict[str, list[list[str]]] = {}
    for item in functions:
        parts = item.split("|")
        by_name.setdefault(parts[0], []).append(parts)
    for name, args in QUEUE_FUNCTIONS.items():
        rows = by_name.get(name, [])
        if len(rows) != 1:
            found.append(f"public.{name} must exist exactly once, found {len(rows)}")
            continue
        acl = EXPECTED_ACL
        if name == "queue_enqueue" and rows[0][-1] == ENQUEUE_INGEST_ACL:
            acl = ENQUEUE_INGEST_ACL
        expected = [
            name,
            args,
            "true",
            QUEUE_ROLE,
            SEARCH_PATH,
            QUEUE_VERSION,
            QUEUE_DIGESTS.get(name, ""),
            acl,
        ]
        if rows[0] != expected:
            found.append(
                f"public.{name} differs from the shipped definition (owner, SECURITY DEFINER, "
                "search_path, version, md5 or ACL)"
            )
    for name in WITHINGS_FUNCTION_NAMES:
        rows = by_name.get(name, [])
        if not rows:
            continue
        args, owner, config, acl = WITHINGS_PINS[name]
        expected = [name, args, "true", owner, config, WITHINGS_VERSION, QUEUE_DIGESTS[name], acl]
        if len(rows) != 1 or rows[0] != expected:
            found.append(
                f"public.{name} differs from the shipped definition (owner, SECURITY DEFINER, "
                "settings, version, md5 or ACL)"
            )
    for name in RATE_FUNCTION_NAMES:
        rows = by_name.get(name, [])
        if not rows:
            continue
        args, owner, config, acl = RATE_PINS[name]
        expected = [name, args, "true", owner, config, RATE_VERSION, QUEUE_DIGESTS[name], acl]
        if len(rows) != 1 or rows[0] != expected:
            found.append(
                f"public.{name} differs from the shipped definition (owner, SECURITY DEFINER, "
                "settings, version, md5 or ACL)"
            )
    return found


def raise_if_drifted(row: Sequence[Any] | None) -> None:
    found = queue_problems(row)
    if found:
        raise QueueDriftError(
            "the job queue does not match ADR 0005 section 3: " + "; ".join(found)
        )


_verified: "weakref.WeakKeyDictionary[Engine, bool]" = weakref.WeakKeyDictionary()


def install_queue_check(engine: AsyncEngine) -> None:
    """Run the queue check on every new connection of ``engine`` (after the role check that
    ``create_engine`` installed); a drifted queue raises ``QueueDriftError`` there, so the pool
    never hands such a connection out. ``queue_verified(engine)`` turns true once one passed."""
    sync_engine = engine.sync_engine
    if sync_engine in _verified:
        return
    _verified[sync_engine] = False

    @event.listens_for(sync_engine, "connect")
    def check_queue(dbapi_connection: Any, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(QUEUE_QUERY)
            row = cursor.fetchone()
        finally:
            cursor.close()
        dbapi_connection.rollback()
        raise_if_drifted(row)
        _verified[sync_engine] = True


def queue_verified(engine: AsyncEngine) -> bool:
    """True once a connection of ``engine`` passed the queue check. The worker claims nothing
    before that (ADR 0005 section 3): a database that was down at startup was not checked yet."""
    return _verified.get(engine.sync_engine, False)


async def verify_queue(engine: AsyncEngine) -> None:
    """The startup check (ADR 0005 section 3): refuse to start on a drifted queue.

    ``engine`` must have the per-connection check installed (``install_queue_check``); this also
    checks a connection that was pooled before. An unreachable database is not an error here: the
    per-connection check applies to every connection made later, and ``queue_verified`` stays
    false until one passed."""
    if engine.sync_engine not in _verified:
        raise RuntimeError("install_queue_check(engine) first")
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(QUEUE_QUERY))).one()
    except OperationalError as exc:
        if is_authentication_failure(exc):
            raise DatabaseRoleError(
                "the database refused the configured credentials; check database_url and the "
                "database_password secret file"
            ) from None
        logger.debug("database unreachable at startup; queue checked on first connection")
        return
    raise_if_drifted(row)
    _verified[engine.sync_engine] = True
