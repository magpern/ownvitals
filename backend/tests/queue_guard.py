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

from ownvitals.db.queue_check import QUEUE_FUNCTIONS, QUEUE_VERSION
from ownvitals.db.queue_digests import QUEUE_DIGESTS

QUEUE, APP = "ownvitals_queue", "ownvitals_app"
SEARCH_PATH = ["search_path=pg_catalog, pg_temp"]
ACL = {f"{QUEUE}=X/{QUEUE}", f"{APP}=X/{QUEUE}"}
TABLES = {
    "jobs": {"SELECT", "UPDATE"},
    "job_events": {"SELECT", "INSERT"},
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
    columns = conn.execute(
        text("SELECT count(*) FROM pg_attribute t, aclexplode(t.attacl) a WHERE a.grantee = :oid"),
        {"oid": role.oid},
    ).scalar_one()
    need(columns == 0, f"{QUEUE} must hold no column privileges")
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
    # aggregate or operator in any schema besides the three, no trigger but the internal ones of
    # foreign keys, no rule but the _RETURN rules of views. Named as the startup check and the
    # script name them.
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
            {"names": list(QUEUE_FUNCTIONS), "system": list(SYSTEM_SCHEMAS)},
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
            "obj_description(p.oid, 'pg_proc') AS comment, md5(pg_get_functiondef(p.oid)) AS md5 "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_language l ON l.oid = p.prolang WHERE p.proname = ANY(:names)"
        ),
        {"names": list(QUEUE_FUNCTIONS)},
    ).all()
    need(
        sorted(str(c) for c, _ in owned) == ["pg_proc"] * len(QUEUE_FUNCTIONS)
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
        need(set(f.acl or []) == ACL and len(f.acl or []) == 2, f"{prefix}: ACL must be {ACL}")
    return problems
