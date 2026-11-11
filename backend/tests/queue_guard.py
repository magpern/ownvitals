"""Guard 9 (ADR 0005 section 3): the queue role and its SECURITY DEFINER functions.

``check_queue`` returns a list of violations (empty when the database matches the ADR). It is a
second implementation of what the worker checks at startup (``ownvitals.db.queue_check``), read
through the superuser so it can also see ``pg_authid``; only the shipped constants (version,
digests, search_path) are shared.

Until #77 item 2 teaches guard 2 (``role_guard.py``) the queue role and the app role's
``EXECUTE`` on the queue functions, this guard is the only one that covers them, and the tests
uninstall the queue after each module (``jobtools``).
"""

import re

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ownvitals.db.outbox_digests import OUTBOX_DIGESTS
from ownvitals.db.queue_check import (
    ADMIN_ACCOUNTS_FUNCTION_NAMES,
    ADMIN_ACCOUNTS_PINS,
    ADMIN_ACCOUNTS_VERSION,
    ADMIN_REVOKE_ENQUEUE,
    ADMIN_REVOKE_ENQUEUE_ACL,
    ADMIN_REVOKE_ENQUEUE_ADMIN_ACL,
    ADMIN_REVOKE_ENQUEUE_ARGS,
    AI_FUNCTION_NAMES,
    AI_PINS,
    AI_VERSION,
    EXPORTS_FUNCTION_NAMES,
    EXPORTS_PINS,
    EXPORTS_VERSION,
    OUTBOX_FUNCTION_NAMES,
    OUTBOX_PINS,
    OUTBOX_VERSION,
    PURGE_ACCOUNT_ADMIN_ACL,
    QUEUE_FUNCTIONS,
    QUEUE_VERSION,
    RATE_FUNCTION_NAMES,
    RATE_PINS,
    RATE_VERSION,
    WITHINGS_FUNCTION_NAMES,
    WITHINGS_PINS,
    WITHINGS_VERSION,
)
from ownvitals.db.queue_digests import QUEUE_DIGESTS

QUEUE, APP = "ownvitals_queue", "ownvitals_app"
SEARCH_PATH = ["search_path=pg_catalog, pg_temp"]
ACL = {f"{QUEUE}=X/{QUEUE}", f"{APP}=X/{QUEUE}"}
TABLES = {
    "jobs": {"SELECT", "INSERT", "UPDATE"},
    "job_events": {"SELECT", "INSERT"},
}
# Enqueue reads the connection's owner and status and nothing else (no secret column).
CONNECTION_COLUMNS = {
    ("public.connections", "id", "SELECT"),
    ("public.connections", "owner_id", "SELECT"),
    ("public.connections", "status", "SELECT"),
}
# Schemas whose functions and operators below FirstNormalObjectId (16384) are initdb's built-ins.
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")
# Every catalog with an ACL column (name, ACL column, how to name the object).
ACL_CATALOGS = (
    ("pg_database", "datacl", "datname"),
    ("pg_namespace", "nspacl", "nspname"),
    ("pg_class", "relacl", "relnamespace::regnamespace::text || '.' || relname"),
    ("pg_proc", "proacl", "oid::regprocedure::text"),
    ("pg_language", "lanacl", "lanname"),
    ("pg_type", "typacl", "oid::regtype::text"),
    ("pg_largeobject_metadata", "lomacl", "oid::text"),
    ("pg_foreign_data_wrapper", "fdwacl", "fdwname"),
    ("pg_foreign_server", "srvacl", "srvname"),
    ("pg_tablespace", "spcacl", "spcname"),
    ("pg_parameter_acl", "paracl", "parname"),
)

# Built-ins that run SQL text, read whole tables or change settings, by name in pg_catalog (every
# overload); written apart from ownvitals.db.queue_check's list on purpose.
SQL_TEXT_BUILTINS = frozenset(
    {f"{prefix}_to_{suffix}" for prefix in ("query", "table", "cursor", "schema", "database")
     for suffix in ("xml", "xmlschema", "xml_and_xmlschema")}
    - {"cursor_to_xml_and_xmlschema"}
    | {"ts_stat", "ts_rewrite", "set_config"}
)  # fmt: skip
# Every catalog column holding an expression tree a write or a type can evaluate.
EXPRESSION_SOURCES = (
    "SELECT 'the default of ' || adrelid::regclass::text || '.' || "
    "(SELECT attname FROM pg_attribute WHERE attrelid = adrelid AND attnum = adnum), "
    "adbin::text FROM pg_attrdef",
    "SELECT 'constraint ' || conname, conbin::text FROM pg_constraint WHERE conbin IS NOT NULL",
    "SELECT 'index ' || indexrelid::regclass::text, indexprs::text FROM pg_index "
    "WHERE indexprs IS NOT NULL",
    "SELECT 'index ' || indexrelid::regclass::text, indpred::text FROM pg_index "
    "WHERE indpred IS NOT NULL",
    "SELECT 'the default of type ' || oid::regtype::text, typdefaultbin::text FROM pg_type "
    "WHERE typdefaultbin IS NOT NULL",
    "SELECT 'the partition key of ' || partrelid::regclass::text, partexprs::text "
    "FROM pg_partitioned_table WHERE partexprs IS NOT NULL",
)
FUNCID = re.compile(r":funcid ([0-9]+)")


def expression_calls(conn: Connection) -> list[str]:
    """``built-in <name> in <where>`` for each stored expression calling one of
    ``SQL_TEXT_BUILTINS``."""
    refused = dict(
        conn.execute(
            text(
                "SELECT p.oid, p.proname FROM pg_proc p WHERE p.proname = ANY(:names) "
                "AND p.pronamespace = 'pg_catalog'::regnamespace"
            ),
            {"names": sorted(SQL_TEXT_BUILTINS)},
        ).all()
    )
    found: set[str] = set()
    for source in EXPRESSION_SOURCES:
        for where, tree in conn.execute(text(source)).all():
            for oid in FUNCID.findall(tree):
                if int(oid) in refused:
                    found.add(f"built-in {refused[int(oid)]} in {where}")
    return sorted(found)


def check_queue(conn: Connection) -> list[str]:
    problems: list[str] = []

    def need(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    role = conn.execute(text("SELECT * FROM pg_authid WHERE rolname = :q"), {"q": QUEUE}).first()
    if role is None:
        return [f"{QUEUE} does not exist"]
    need(not role.rolcanlogin, f"{QUEUE} must be NOLOGIN")
    need(role.rolpassword is None, f"{QUEUE} must have no password")
    need(role.rolbypassrls, f"{QUEUE} must have BYPASSRLS (it owns the cross-user functions)")
    need(not role.rolinherit, f"{QUEUE} must be NOINHERIT")
    for attribute in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication"):
        need(not getattr(role, attribute), f"{QUEUE} must not have {attribute}")
    members = conn.execute(
        text("SELECT count(*) FROM pg_auth_members WHERE :oid IN (member, roleid)"),
        {"oid": role.oid},
    ).scalar_one()
    need(members == 0, f"{QUEUE} must be a member of no role and have no members")
    settings = conn.execute(
        text("SELECT count(*) FROM pg_db_role_setting WHERE setrole = :oid"), {"oid": role.oid}
    ).scalar_one()
    need(settings == 0, f"{QUEUE} must have no role settings")
    default_acls = conn.execute(
        text(
            "SELECT count(*) FROM pg_default_acl d WHERE d.defaclrole = :oid OR EXISTS "
            "(SELECT 1 FROM aclexplode(d.defaclacl) a WHERE a.grantee = :oid)"
        ),
        {"oid": role.oid},
    ).scalar_one()
    need(default_acls == 0, f"{QUEUE} must appear in no default privileges")

    # Privileges held, over every ACL kind of this database and the shared catalogs.
    held: set[tuple[str, str, str]] = set()
    for catalog, column, name in ACL_CATALOGS:
        for obj, privilege, grantable in conn.execute(
            text(
                f"SELECT {name}, a.privilege_type, a.is_grantable "  # noqa: S608
                f"FROM pg_catalog.{catalog} c, aclexplode(c.{column}) a "
                "WHERE a.grantee = :oid AND NOT (:catalog = 'pg_proc' AND c.oid IN "
                "(SELECT oid FROM pg_proc WHERE proowner = :oid))"
            ),
            {"oid": role.oid, "catalog": catalog},
        ).all():
            need(not grantable, f"{QUEUE}: grantable {privilege} on {catalog} {obj}")
            held.add((catalog, str(obj), str(privilege)))
    columns = {
        (str(table), str(column), str(privilege))
        for table, column, privilege in conn.execute(
            text(
                "SELECT n.nspname || '.' || c.relname, t.attname, a.privilege_type "
                "FROM pg_attribute t JOIN pg_class c ON c.oid = t.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(t.attacl) a "
                "WHERE a.grantee = :oid"
            ),
            {"oid": role.oid},
        ).all()
    }
    need(
        columns == CONNECTION_COLUMNS,
        f"{QUEUE} column privileges must be exactly {sorted(CONNECTION_COLUMNS)}, are {columns}",
    )
    expected = {("pg_namespace", "public", "USAGE")} | {
        ("pg_class", f"public.{table}", privilege)
        for table, privileges in TABLES.items()
        for privilege in privileges
    }
    need(held == expected, f"{QUEUE} privileges must be exactly {sorted(expected)}, are {held}")
    other_databases = conn.execute(
        text(
            "SELECT count(*) FROM pg_shdepend WHERE refobjid = :oid AND dbid NOT IN "
            "(0, (SELECT oid FROM pg_database WHERE datname = current_database()))"
        ),
        {"oid": role.oid},
    ).scalar_one()
    need(other_databases == 0, f"{QUEUE} must own or hold nothing in another database")
    policies = conn.execute(
        text("SELECT count(*) FROM pg_policy WHERE :oid = ANY(polroles)"), {"oid": role.oid}
    ).scalar_one()
    need(policies == 0, f"{QUEUE} must be named in no policy")

    # Nothing that could shadow a built-in called by a function body, and nothing a function body
    # fires that could run user code as ownvitals_queue: no user-defined function, procedure,
    # aggregate or operator in any schema besides the queue functions and the Withings
    # functions, no trigger but the internal ones of foreign keys, no rule but the _RETURN rules
    # of views. Named as the startup check and the script name them.
    admitted = [
        *QUEUE_FUNCTIONS,
        ADMIN_REVOKE_ENQUEUE,
        *WITHINGS_FUNCTION_NAMES,
        *RATE_FUNCTION_NAMES,
        *OUTBOX_FUNCTION_NAMES,
        *AI_FUNCTION_NAMES,
        *EXPORTS_FUNCTION_NAMES,
        *ADMIN_ACCOUNTS_FUNCTION_NAMES,
    ]
    hooks = (
        conn.execute(
            text(
                "SELECT 'function ' || n.nspname || '.' || p.proname "
                "|| '(' || oidvectortypes(p.proargtypes) || ')' FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE NOT (n.nspname = 'public' AND p.proname = ANY(:names)) "
                "AND (p.oid >= 16384 OR NOT n.nspname = ANY(:system)) "
                "UNION ALL SELECT 'operator ' || n.nspname || '.' || o.oprname || '(' "
                "|| CASE WHEN o.oprleft = 0 THEN 'NONE' ELSE format_type(o.oprleft, NULL) END "
                "|| ', ' "
                "|| CASE WHEN o.oprright = 0 THEN 'NONE' ELSE format_type(o.oprright, NULL) END "
                "|| ')' FROM pg_operator o "
                "JOIN pg_namespace n ON n.oid = o.oprnamespace "
                "WHERE o.oid >= 16384 OR NOT n.nspname = ANY(:system) "
                "UNION ALL SELECT 'trigger ' || t.tgname || ' on ' || n.nspname || '.' "
                "|| c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE NOT t.tgisinternal "
                "UNION ALL SELECT 'rule ' || r.rulename || ' on ' || n.nspname || '.' "
                "|| c.relname FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE r.rulename <> '_RETURN' AND r.oid >= 16384"
            ),
            {"names": admitted, "system": list(SYSTEM_SCHEMAS)},
        )
        .scalars()
        .all()
    )
    # Temp schemas (pg_temp_N) hold no built-ins, so any function there is >= 16384 and found.
    need(
        not hooks,
        f"user-defined functions, operators, triggers or rules besides the queue's: {hooks}",
    )
    calls = expression_calls(conn)
    need(not calls, f"expressions call a built-in that runs SQL text: {calls}")

    # Ownership: exactly the three functions.
    owned = conn.execute(
        text(
            "SELECT classid::regclass::text, objid FROM pg_shdepend "
            "WHERE refobjid = :oid AND deptype = 'o'"
        ),
        {"oid": role.oid},
    ).all()
    functions = conn.execute(
        text(
            "SELECT p.oid, n.nspname, p.proname, "
            "pg_get_function_identity_arguments(p.oid) AS args, "
            "p.prosecdef, p.prokind, l.lanname, p.proowner, p.proconfig, p.proacl::text[] AS acl, "
            "obj_description(p.oid, 'pg_proc') AS comment, md5(pg_get_functiondef(p.oid)) AS md5, "
            "pg_get_userbyid(p.proowner) AS owner_name "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang WHERE p.proname = ANY(:names)"
        ),
        {"names": admitted},
    ).all()
    need(
        sorted(str(c) for c, _ in owned) == ["pg_proc"] * (len(QUEUE_FUNCTIONS) + 1)
        and {o for _, o in owned} == {f.oid for f in functions if f.proowner == role.oid},
        f"{QUEUE} must own exactly the queue functions",
    )
    for name, args in QUEUE_FUNCTIONS.items():
        found = [f for f in functions if f.proname == name]
        if len(found) != 1:
            problems.append(f"{name}: must exist exactly once (in any schema), found {len(found)}")
            continue
        f = found[0]
        prefix = f"public.{name}"
        need(f.nspname == "public" and f.args == args, f"{prefix}: wrong schema or arguments")
        need(f.prosecdef, f"{prefix}: must be SECURITY DEFINER")
        need(f.prokind == "f" and f.lanname == "plpgsql", f"{prefix}: must be a plpgsql function")
        need(f.proowner == role.oid, f"{prefix}: must be owned by {QUEUE}")
        need(list(f.proconfig or []) == SEARCH_PATH, f"{prefix}: search_path must be fixed")
        need(f.comment == QUEUE_VERSION, f"{prefix}: comment must be {QUEUE_VERSION!r}")
        need(f.md5 == QUEUE_DIGESTS[name], f"{prefix}: definition md5 differs from the shipped one")
        actual_acl = set(f.acl or [])
        expected_acl = set(ACL)
        # The ingest role must EXECUTE queue_enqueue once 40-withings.sql installed the ingest,
        # and must not before (#280). 20-queue.sql run alone keeps the grant.
        if name == "queue_enqueue" and any(g.proname == "webhook_event_insert" for g in functions):
            expected_acl.add(f"ownvitals_webhook_events=X/{QUEUE}")
        need(
            actual_acl == expected_acl and len(actual_acl) == len(expected_acl),
            f"{prefix}: ACL must be {expected_acl}, is {actual_acl}",
        )
    admin_installed = any(f.proname in ADMIN_ACCOUNTS_FUNCTION_NAMES for f in functions)
    # #365: the admin delete's revoke enqueue, a queue function only the admin family's role runs
    # (and nobody but its owner before that family is installed).
    found = [f for f in functions if f.proname == ADMIN_REVOKE_ENQUEUE]
    prefix = f"public.{ADMIN_REVOKE_ENQUEUE}"
    if len(found) != 1:
        problems.append(f"{ADMIN_REVOKE_ENQUEUE}: must exist exactly once, found {len(found)}")
    else:
        f = found[0]
        need(
            f.nspname == "public" and f.args == ADMIN_REVOKE_ENQUEUE_ARGS,
            f"{prefix}: wrong schema or arguments",
        )
        need(f.prosecdef, f"{prefix}: must be SECURITY DEFINER")
        need(f.prokind == "f" and f.lanname == "plpgsql", f"{prefix}: must be a plpgsql function")
        need(f.proowner == role.oid, f"{prefix}: must be owned by {QUEUE}")
        need(list(f.proconfig or []) == SEARCH_PATH, f"{prefix}: search_path must be fixed")
        need(f.comment == QUEUE_VERSION, f"{prefix}: comment must be {QUEUE_VERSION!r}")
        need(
            f.md5 == QUEUE_DIGESTS[ADMIN_REVOKE_ENQUEUE],
            f"{prefix}: definition md5 differs from the shipped one",
        )
        acl = ADMIN_REVOKE_ENQUEUE_ADMIN_ACL if admin_installed else ADMIN_REVOKE_ENQUEUE_ACL
        expected_acl = set(acl.split(","))
        need(
            set(f.acl or []) == expected_acl and len(f.acl or []) == len(expected_acl),
            f"{prefix}: ACL must be {expected_acl}",
        )
    for name in WITHINGS_FUNCTION_NAMES:
        found = [f for f in functions if f.proname == name]
        if not found:
            continue
        args, owner, config, acl = WITHINGS_PINS[name]
        if name == "webhook_events_purge_account" and admin_installed:
            acl = PURGE_ACCOUNT_ADMIN_ACL  # the admin account delete's alone (#314)
        if len(found) != 1:
            problems.append(f"{name}: must exist exactly once, found {len(found)}")
            continue
        f = found[0]
        prefix = f"public.{name}"
        need(f.nspname == "public" and f.args == args, f"{prefix}: wrong schema or arguments")
        need(f.prosecdef, f"{prefix}: must be SECURITY DEFINER")
        need(f.prokind == "f" and f.lanname == "plpgsql", f"{prefix}: must be a plpgsql function")
        need(f.owner_name == owner, f"{prefix}: must be owned by {owner}")
        need(list(f.proconfig or []) == config.split(";"), f"{prefix}: settings must be fixed")
        need(f.comment == WITHINGS_VERSION, f"{prefix}: comment must be {WITHINGS_VERSION!r}")
        need(f.md5 == QUEUE_DIGESTS[name], f"{prefix}: definition md5 differs from the shipped one")
        expected_acl = set(acl.split(","))
        need(
            set(f.acl or []) == expected_acl and len(f.acl or []) == len(expected_acl),
            f"{prefix}: ACL must be {expected_acl}",
        )
    for name in RATE_FUNCTION_NAMES:
        found = [f for f in functions if f.proname == name]
        if not found:
            continue
        args, owner, config, acl = RATE_PINS[name]
        if len(found) != 1:
            problems.append(f"{name}: must exist exactly once, found {len(found)}")
            continue
        f = found[0]
        prefix = f"public.{name}"
        need(f.nspname == "public" and f.args == args, f"{prefix}: wrong schema or arguments")
        need(f.prosecdef, f"{prefix}: must be SECURITY DEFINER")
        need(f.prokind == "f" and f.lanname == "plpgsql", f"{prefix}: must be a plpgsql function")
        need(f.owner_name == owner, f"{prefix}: must be owned by {owner}")
        need(list(f.proconfig or []) == config.split(";"), f"{prefix}: settings must be fixed")
        need(f.comment == RATE_VERSION, f"{prefix}: comment must be {RATE_VERSION!r}")
        need(f.md5 == QUEUE_DIGESTS[name], f"{prefix}: definition md5 differs from the shipped one")
        expected_acl = set(acl.split(","))
        need(
            set(f.acl or []) == expected_acl and len(f.acl or []) == len(expected_acl),
            f"{prefix}: ACL must be {expected_acl}",
        )
    for name in OUTBOX_FUNCTION_NAMES:
        found = [f for f in functions if f.proname == name]
        if not found:
            continue
        args, owner, config, acl = OUTBOX_PINS[name]
        if len(found) != 1:
            problems.append(f"{name}: must exist exactly once, found {len(found)}")
            continue
        f = found[0]
        prefix = f"public.{name}"
        need(f.nspname == "public" and f.args == args, f"{prefix}: wrong schema or arguments")
        need(f.prosecdef, f"{prefix}: must be SECURITY DEFINER")
        need(f.prokind == "f" and f.lanname == "plpgsql", f"{prefix}: must be a plpgsql function")
        need(f.owner_name == owner, f"{prefix}: must be owned by {owner}")
        need(list(f.proconfig or []) == config.split(";"), f"{prefix}: settings must be fixed")
        need(f.comment == OUTBOX_VERSION, f"{prefix}: comment must be {OUTBOX_VERSION!r}")
        need(
            f.md5 == OUTBOX_DIGESTS[name], f"{prefix}: definition md5 differs from the shipped one"
        )
        expected_acl = set(acl.split(","))
        need(
            set(f.acl or []) == expected_acl and len(f.acl or []) == len(expected_acl),
            f"{prefix}: ACL must be {expected_acl}",
        )
    # 50-ai.sql (#205, #331, ADR 0014 s. 6 and 7): absent, or all five functions, each pinned.
    ai_installed = any(f.proname in AI_FUNCTION_NAMES for f in functions)
    for name in AI_FUNCTION_NAMES if ai_installed else ():
        found = [f for f in functions if f.proname == name]
        args, owner, config, acl = AI_PINS[name]
        if len(found) != 1:
            problems.append(f"{name}: must exist exactly once, found {len(found)}")
            continue
        f = found[0]
        prefix = f"public.{name}"
        need(f.nspname == "public" and f.args == args, f"{prefix}: wrong schema or arguments")
        need(f.prosecdef, f"{prefix}: must be SECURITY DEFINER")
        need(f.prokind == "f" and f.lanname == "plpgsql", f"{prefix}: must be a plpgsql function")
        need(f.owner_name == owner, f"{prefix}: must be owned by {owner}")
        need(list(f.proconfig or []) == config.split(";"), f"{prefix}: settings must be fixed")
        need(f.comment == AI_VERSION, f"{prefix}: comment must be {AI_VERSION!r}")
        need(f.md5 == QUEUE_DIGESTS[name], f"{prefix}: definition md5 differs from the shipped one")
        expected_acl = set(acl.split(","))
        need(
            set(f.acl or []) == expected_acl and len(f.acl or []) == len(expected_acl),
            f"{prefix}: ACL must be {expected_acl}",
        )
    # 60-exports.sql (#333, ADR 0013 s. 4): absent, or the purge exactly once, pinned.
    for name in EXPORTS_FUNCTION_NAMES:
        found = [f for f in functions if f.proname == name]
        if not found:
            continue
        args, owner, config, acl = EXPORTS_PINS[name]
        if len(found) != 1:
            problems.append(f"{name}: must exist exactly once, found {len(found)}")
            continue
        f = found[0]
        prefix = f"public.{name}"
        need(f.nspname == "public" and f.args == args, f"{prefix}: wrong schema or arguments")
        need(f.prosecdef, f"{prefix}: must be SECURITY DEFINER")
        need(f.prokind == "f" and f.lanname == "plpgsql", f"{prefix}: must be a plpgsql function")
        need(f.owner_name == owner, f"{prefix}: must be owned by {owner}")
        need(list(f.proconfig or []) == config.split(";"), f"{prefix}: settings must be fixed")
        need(f.comment == EXPORTS_VERSION, f"{prefix}: comment must be {EXPORTS_VERSION!r}")
        need(f.md5 == QUEUE_DIGESTS[name], f"{prefix}: definition md5 differs from the shipped one")
        expected_acl = set(acl.split(","))
        need(
            set(f.acl or []) == expected_acl and len(f.acl or []) == len(expected_acl),
            f"{prefix}: ACL must be {expected_acl}",
        )
    # 70-admin-accounts.sql (#314, ADR 0002 s. 3): absent, or all three functions, each pinned.
    for name in ADMIN_ACCOUNTS_FUNCTION_NAMES if admin_installed else ():
        found = [f for f in functions if f.proname == name]
        args, owner, config, acl = ADMIN_ACCOUNTS_PINS[name]
        if len(found) != 1:
            problems.append(f"{name}: must exist exactly once, found {len(found)}")
            continue
        f = found[0]
        prefix = f"public.{name}"
        need(f.nspname == "public" and f.args == args, f"{prefix}: wrong schema or arguments")
        need(f.prosecdef, f"{prefix}: must be SECURITY DEFINER")
        need(f.prokind == "f" and f.lanname == "plpgsql", f"{prefix}: must be a plpgsql function")
        need(f.owner_name == owner, f"{prefix}: must be owned by {owner}")
        need(list(f.proconfig or []) == config.split(";"), f"{prefix}: settings must be fixed")
        need(
            f.comment == ADMIN_ACCOUNTS_VERSION,
            f"{prefix}: comment must be {ADMIN_ACCOUNTS_VERSION!r}",
        )
        need(f.md5 == QUEUE_DIGESTS[name], f"{prefix}: definition md5 differs from the shipped one")
        expected_acl = set(acl.split(","))
        need(
            set(f.acl or []) == expected_acl and len(f.acl or []) == len(expected_acl),
            f"{prefix}: ACL must be {expected_acl}",
        )
    return problems
