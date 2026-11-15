"""What the queue check (``ownvitals.db.queue_check``, ADR 0005 section 3, guard 9) expects: the
queue functions and those admitted beside them, their owners, versions, ACLs and pins, the
queue role's attributes, dependencies and privileges, and the one catalogue query that reads it
all. ``ownvitals.db.queue_check`` re-exports every public name here.
"""

from ownvitals.db.queue_expected_extra import (
    ADMIN_ACCOUNTS_FUNCTION_NAMES,
    ADMIN_REVOKE_ENQUEUE,
    ADMIN_REVOKE_ENQUEUE_ARGS,
    AI_FUNCTION_NAMES,
    EXPORTS_FUNCTION_NAMES,
    OUTBOX_FUNCTION_NAMES,
    PURGE_ACCOUNT_ACL,
    RATE_FUNCTION_NAMES,
)

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
    "queue_schedule": "p_kind text, p_interval_seconds integer",
}
# Admitted beside the queue (20-queue.sql). A queue-only database does not have them; a deploy
# installs them and the check then pins each body the same way as a queue function.
WITHINGS_FUNCTION_NAMES = (
    "ensure_month_partitions",
    "webhook_secrets_apply",
    "webhook_event_insert",
    "webhook_events_purge_owner",
    # #362: the admin delete's purge, system session only (the owner is an argument).
    "webhook_events_purge_account",
    # #167: the metrics aggregates, owned by the read-only ownvitals_metrics.
    "metrics_withings_aggregates",
    "metrics_webhook_ages",
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
    "webhook_events_purge_owner": (
        "",
        "ownvitals_webhook_events",
        "search_path=pg_catalog, pg_temp",
        "ownvitals_app=X/ownvitals_webhook_events,ownvitals_webhook_events=X/ownvitals_webhook_events",
    ),
    # The ACL depends on the admin account family (#314): queue_check picks PURGE_ACCOUNT_ACL or
    # PURGE_ACCOUNT_ADMIN_ACL (queue_expected_extra); this entry is the one without it.
    "webhook_events_purge_account": (
        "p_owner uuid",
        "ownvitals_webhook_events",
        "search_path=pg_catalog, pg_temp",
        PURGE_ACCOUNT_ACL,
    ),
    "metrics_withings_aggregates": (
        "",
        "ownvitals_metrics",
        "search_path=pg_catalog, pg_temp",
        "ownvitals_app=X/ownvitals_metrics,ownvitals_metrics=X/ownvitals_metrics",
    ),
    "metrics_webhook_ages": (
        "p_after timestamp with time zone, p_after_id uuid",
        "ownvitals_metrics",
        "search_path=pg_catalog, pg_temp",
        "ownvitals_app=X/ownvitals_metrics,ownvitals_metrics=X/ownvitals_metrics",
    ),
}
EXPECTED_ACL = "ownvitals_app=X/ownvitals_queue,ownvitals_queue=X/ownvitals_queue"
# 40-withings.sql grants the ingest role EXECUTE on queue_enqueue so the notification and the
# job commit together, and 20-queue.sql keeps that grant when it is re-run on its own. The ACL
# is this one exactly when webhook_event_insert exists, and EXPECTED_ACL before it does (#280).
ENQUEUE_INGEST_ACL = EXPECTED_ACL + ",ownvitals_webhook_events=X/ownvitals_queue"
# rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit
EXPECTED_ATTRIBUTES = [False, False, False, False, False, True, False]
# What ownvitals_queue may hold: ACL entries ("a") and ownership ("o"), as pg_shdepend lists them.
EXPECTED_DEPENDENCIES = frozenset(
    {f"pg_proc:public.{name}({args}):0:o" for name, args in QUEUE_FUNCTIONS.items()}
    | {f"pg_proc:public.{ADMIN_REVOKE_ENQUEUE}({ADMIN_REVOKE_ENQUEUE_ARGS}):0:o"}  # #365
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
    f"'{name}'"
    for name in (
        *QUEUE_FUNCTIONS,
        ADMIN_REVOKE_ENQUEUE,
        *WITHINGS_FUNCTION_NAMES,
        *RATE_FUNCTION_NAMES,
        *OUTBOX_FUNCTION_NAMES,
        *AI_FUNCTION_NAMES,
        *EXPORTS_FUNCTION_NAMES,
        *ADMIN_ACCOUNTS_FUNCTION_NAMES,
    )
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
