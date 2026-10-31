"""Guard 2 of ADR 0002 section 7: roles, privileges and defaults, checked as an allowlist.

``check_roles`` returns a list of violations (empty when the database matches section 5). It
reads ``pg_roles``, ``pg_auth_members``, ``pg_shdepend`` and the ACLs through ``aclexplode``, so
a privilege of a kind nobody thought of still fails.

The function owners of ADR 0005 section 3 (``ownvitals_queue``, #9) and the admin role
``ownvitals_maint`` (#12) are not here yet: each issue adds its role as an allowed grantee, with
exactly its privileges, when it creates it. Until then any other grantee fails.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ownvitals.db.role_check import EXPECTED_ROLE_SETTINGS
from ownvitals.db.tables import NO_APP_ACCESS

APP, OWNER = "ownvitals_app", "ownvitals_owner"
GRANTEES = {APP, OWNER}
TABLE_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "DELETE"}
# Tables where the app role gets less than full DML, by table; each migration that narrows a
# table's grants adds it here (and to 10-roles.sql's restore list). ``NO_APP_ACCESS`` tables get
# nothing at all.
RESTRICTED_APP_PRIVILEGES: dict[str, set[str]] = {}
# The only column-level privileges in public, exactly: (table, column) -> {(grantee, privilege)}.
COLUMN_PRIVILEGES: dict[tuple[str, str], set[tuple[str, str]]] = {}
SEQUENCE_PRIVILEGES = {"USAGE", "SELECT"}
# Built-in languages that PUBLIC may use (Postgres' own default).
BUILTIN_LANGUAGES = {"plpgsql", "sql", "internal", "c"}
EXPECTED_TIMEOUTS = EXPECTED_ROLE_SETTINGS
GRANTEE = "CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END"
Acl = list[tuple[str, str, str, bool]]


def _acl(conn: Connection, sql: str) -> Acl:
    """Rows of (object, grantee, privilege, grantable) from ``aclexplode``."""
    return [
        (str(o), str(g), str(p), bool(x))
        for o, g, p, x in conn.execute(text(sql.replace("{G}", GRANTEE))).all()
    ]


def _privileges(rows: Acl, grantee: str, obj: str) -> set[str]:
    return {p for o, g, p, _ in rows if o == obj and g == grantee}


def _grantees(rows: Acl, obj: str | None = None) -> set[str]:
    return {g for o, g, _, _ in rows if obj is None or o == obj}


def check_roles(conn: Connection) -> list[str]:
    problems: list[str] = []

    def need(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    # Role attributes, memberships and ownership.
    roles = {
        r.rolname: r
        for r in conn.execute(
            text("SELECT * FROM pg_roles WHERE rolname IN (:app, :owner)"),
            {"app": APP, "owner": OWNER},
        ).all()
    }
    if set(roles) != {APP, OWNER}:
        return ["ownvitals_app and ownvitals_owner must both exist"]
    for name, role in roles.items():
        need(role.rolcanlogin, f"{name} must be able to log in")
        for attribute in (
            "rolsuper",
            "rolbypassrls",
            "rolcreatedb",
            "rolcreaterole",
            "rolreplication",
        ):
            need(not getattr(role, attribute), f"{name} must not have {attribute}")
        members = conn.execute(
            text("SELECT count(*) FROM pg_auth_members WHERE member = :oid"), {"oid": role.oid}
        ).scalar_one()
        need(members == 0, f"{name} must be a member of no role")
        privileged = conn.execute(
            text(
                "WITH RECURSIVE r AS (SELECT CAST(:oid AS oid) AS oid "
                "UNION SELECT am.roleid FROM pg_auth_members am JOIN r ON am.member = r.oid) "
                "SELECT count(*) FROM pg_roles x JOIN r ON r.oid = x.oid "
                "WHERE x.rolsuper OR x.rolbypassrls"
            ),
            {"oid": role.oid},
        ).scalar_one()
        need(privileged == 0, f"{name} must not reach a superuser or BYPASSRLS role")
    need(not roles[APP].rolinherit, "ownvitals_app must be NOINHERIT")
    owned_by_app = conn.execute(
        text(
            "SELECT count(*) FROM pg_shdepend WHERE refobjid = :oid AND deptype = 'o' "
            "AND dbid IN (0, (SELECT oid FROM pg_database WHERE datname = current_database()))"
        ),
        {"oid": roles[APP].oid},
    ).scalar_one()
    need(owned_by_app == 0, "ownvitals_app must own nothing")
    # Role settings: exactly the three timeouts, at role level. A per-database setting would take
    # precedence over them, and anything else (session_replication_role, search_path...) changes
    # how the app role's sessions behave.
    setting_rows = conn.execute(
        text("SELECT setdatabase, setconfig FROM pg_db_role_setting WHERE setrole = :oid"),
        {"oid": roles[APP].oid},
    ).all()
    need(
        [row.setdatabase for row in setting_rows] in ([], [0]),
        "ownvitals_app may have role-level settings only, none per database",
    )
    found = dict(item.split("=", 1) for row in setting_rows for item in row.setconfig)
    need(
        found == EXPECTED_TIMEOUTS,
        f"ownvitals_app role settings must be exactly {EXPECTED_TIMEOUTS}, are {found}",
    )

    # The app role can connect to its own database and no other.
    other_databases = (
        conn.execute(
            text(
                "SELECT datname FROM pg_database WHERE datallowconn "
                "AND datname <> current_database() "
                "AND has_database_privilege(:app, oid, 'CONNECT')"
            ),
            {"app": APP},
        )
        .scalars()
        .all()
    )
    need(not other_databases, f"ownvitals_app must not connect to {other_databases}")

    # The database.
    db = conn.execute(text("SELECT current_database()")).scalar_one()
    databases = _acl(
        conn,
        "SELECT d.datname, {G}, a.privilege_type, a.is_grantable FROM pg_database d, "
        "aclexplode(coalesce(d.datacl, acldefault(CAST('d' AS \"char\"), d.datdba))) a "
        "WHERE d.datname = current_database()",
    )
    need(
        _privileges(databases, APP, db) == {"CONNECT"},
        "ownvitals_app: CONNECT only on the database",
    )
    need(not _privileges(databases, "PUBLIC", db), "PUBLIC must have no privilege on the database")
    need("CONNECT" in _privileges(databases, OWNER, db), "ownvitals_owner needs CONNECT")
    need(_grantees(databases) <= GRANTEES, "unexpected grantee on the database")
    database_owner = conn.execute(
        text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")
    ).scalar_one()
    need(database_owner == OWNER, "ownvitals_owner must own the database")

    # Schemas.
    schemas = _acl(
        conn,
        "SELECT n.nspname, {G}, a.privilege_type, a.is_grantable FROM pg_namespace n, "
        "aclexplode(coalesce(n.nspacl, acldefault(CAST('n' AS \"char\"), n.nspowner))) a "
        "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'",
    )
    need(_privileges(schemas, APP, "public") == {"USAGE"}, "ownvitals_app: USAGE only on public")
    need(not _privileges(schemas, "PUBLIC", "public"), "PUBLIC must have no privilege on public")
    need(
        {"USAGE", "CREATE"} <= _privileges(schemas, OWNER, "public"),
        "ownvitals_owner needs USAGE and CREATE on public",
    )
    need(_grantees(schemas) <= GRANTEES, "unexpected grantee on a schema")
    need(
        not [1 for o, g, _, _ in schemas if o != "public" and g in (APP, "PUBLIC")],
        "no privilege for PUBLIC or ownvitals_app on another schema",
    )
    schema_owner = conn.execute(
        text("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'")
    ).scalar_one()
    need(schema_owner == OWNER, "ownvitals_owner must own schema public")

    # Tables and sequences (and views etc., which are not allowed but must not be exposed).
    relations = _acl(
        conn,
        "SELECT c.relname || ':' || c.relkind::text, {G}, a.privilege_type, a.is_grantable "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace, "
        "aclexplode(coalesce(c.relacl, acldefault("
        "CASE WHEN c.relkind = 'S' THEN CAST('s' AS \"char\") "
        "ELSE CAST('r' AS \"char\") END, c.relowner))) a "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')",
    )
    for obj in sorted({o for o, *_ in relations}):
        expected = SEQUENCE_PRIVILEGES if obj.endswith(":S") else TABLE_PRIVILEGES
        expected = RESTRICTED_APP_PRIVILEGES.get(obj.split(":")[0], expected)
        if obj.split(":")[0] in NO_APP_ACCESS:
            expected = set()
        need(
            _privileges(relations, APP, obj) == expected,
            f"{obj}: ownvitals_app privileges must be exactly {sorted(expected)}",
        )
        need(not _privileges(relations, "PUBLIC", obj), f"{obj}: PUBLIC must have no privilege")
        need(_grantees(relations, obj) <= GRANTEES, f"{obj}: unexpected grantee")
    need(
        not [1 for *_, grantable in relations if grantable], "no grantable privilege on a relation"
    )
    foreign_owned = conn.execute(
        text(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') "
            "AND pg_get_userbyid(c.relowner) <> :owner"
        ),
        {"owner": OWNER},
    ).scalar_one()
    need(foreign_owned == 0, "every relation in public must be owned by ownvitals_owner")
    column_rows = conn.execute(
        text(
            "SELECT c.relname, a.attname, "
            + GRANTEE.replace("a.grantee", "x.grantee")
            + ", x.privilege_type, x.is_grantable "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace, aclexplode(a.attacl) x "
            "WHERE n.nspname = 'public' AND a.attacl IS NOT NULL"
        )
    ).all()
    found_columns: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for table, column, grantee, privilege, grantable in column_rows:
        need(not grantable, f"{table}.{column}: no grantable column privilege")
        found_columns.setdefault((str(table), str(column)), set()).add(
            (str(grantee), str(privilege))
        )
    expected_columns = {
        key: value
        for key, value in COLUMN_PRIVILEGES.items()
        if key[0] in {o.split(":")[0] for o, *_ in relations}
    }
    need(
        found_columns == expected_columns,
        f"column-level privileges in public must be exactly {expected_columns}, "
        f"are {found_columns}",
    )

    # Functions (in public, and user-made ones anywhere): PUBLIC and ownvitals_app may call none.
    # #9 allows the queue's SECURITY DEFINER functions (ADR 0005 section 3; guard 9 checks them in
    # full). 16384 is FirstNormalObjectId: every function a user made.
    functions = _acl(
        conn,
        "SELECT p.oid::regprocedure::text || ':' || pg_get_userbyid(p.proowner) || ':' "
        "|| p.prosecdef::text, {G}, a.privilege_type, a.is_grantable "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, "
        "aclexplode(coalesce(p.proacl, acldefault(CAST('f' AS \"char\"), p.proowner))) a "
        "WHERE n.nspname = 'public' OR p.oid >= 16384",
    )
    need(
        "PUBLIC" not in _grantees(functions),
        "functions in public or made by a user must not be executable by PUBLIC",
    )
    for obj in sorted({o for o, *_ in functions}):
        need(not _privileges(functions, APP, obj), f"{obj}: ownvitals_app may execute no function")
        need(_grantees(functions, obj) <= GRANTEES, f"{obj}: unexpected grantee")
    languages = _acl(
        conn,
        "SELECT l.lanname, {G}, a.privilege_type, a.is_grantable FROM pg_language l, "
        "aclexplode(coalesce(l.lanacl, acldefault(CAST('l' AS \"char\"), l.lanowner))) a",
    )
    need(
        not [1 for o, g, *_ in languages if g in ("PUBLIC", APP) and o not in BUILTIN_LANGUAGES],
        "PUBLIC and ownvitals_app may use only the built-in languages",
    )
    parameters = conn.execute(
        text(
            "SELECT count(*) FROM pg_parameter_acl p, aclexplode(p.paracl) a "
            "WHERE a.grantee = 0 OR a.grantee = CAST(:app AS regrole)"
        ),
        {"app": APP},
    ).scalar_one()
    need(parameters == 0, "no parameter privileges for PUBLIC or ownvitals_app")

    # Default privileges: what the owner's future tables, sequences and functions get.
    defaults = [
        (str(r), str(k), str(s), str(g), str(p))
        for r, k, s, g, p in conn.execute(
            text(
                (
                    "SELECT pg_get_userbyid(d.defaclrole), d.defaclobjtype, "
                    "d.defaclnamespace::regnamespace::text, "
                    "{G}, a.privilege_type FROM pg_default_acl d, aclexplode(d.defaclacl) a"
                ).replace("{G}", GRANTEE)
            )
        ).all()
    ]
    expected_defaults = {("r", "public"): TABLE_PRIVILEGES, ("S", "public"): SEQUENCE_PRIVILEGES}
    for (kind, schema), privileges in expected_defaults.items():
        got = {p for r, k, s, g, p in defaults if (r, k, s, g) == (OWNER, kind, schema, APP)}
        need(
            got == privileges,
            f"default privileges on {kind} in {schema}: app must get {sorted(privileges)}",
        )
    function_defaults = [g for r, k, s, g, _ in defaults if (r, k, s) == (OWNER, "f", "-")]
    need(bool(function_defaults), "default privileges for functions must be set")
    need("PUBLIC" not in function_defaults, "new functions must not be executable by PUBLIC")
    allowed = {(OWNER, "r", "public", APP), (OWNER, "S", "public", APP)}
    unexpected = {(r, k, s, g) for r, k, s, g, _ in defaults if g in ("PUBLIC", APP)} - allowed
    need(not unexpected, f"unexpected default privileges: {sorted(unexpected)}")
    return problems
