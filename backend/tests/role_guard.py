"""Guard 2 of ADR 0002 section 7: roles, privileges and defaults, checked as an allowlist.

``check_roles`` returns a list of violations (empty when the database matches section 5). It
reads ``pg_roles``, ``pg_auth_members``, ``pg_shdepend`` and the ACLs through ``aclexplode``, so
a privilege of a kind nobody thought of still fails.

``ownvitals_queue`` (ADR 0005 section 3) is an allowed grantee with exactly the privileges
``docker/postgres/queue/20-queue.sql`` grants, and ``ownvitals_app`` may ``EXECUTE`` the queue
functions. When ``docker/postgres/withings/40-withings.sql`` has run, the webhook NOLOGIN roles
and ``ensure_month_partitions`` hold exactly the grants that script creates, and the app role
may ``EXECUTE`` those three functions as well and no other function. The admin role
``ownvitals_maint`` (#12) is an allowed grantee; guard 11 (``tests/test_maint_privileges.py``)
checks that it holds exactly its allowlist, so its column privileges are left out of the column
check here. Maint is not granted the client secret or ``webhook_secrets``. Any other grantee fails.
"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ownvitals.db.role_check import EXPECTED_ROLE_SETTINGS
from ownvitals.db.tables import NO_APP_ACCESS
from ownvitals.withings.tables import APP_WITHINGS_COLUMNS

APP, OWNER, MAINT = "ownvitals_app", "ownvitals_owner", "ownvitals_maint"
QUEUE = "ownvitals_queue"
GRANTEES = {APP, OWNER, MAINT}
TABLE_PRIVILEGES = {"SELECT", "INSERT", "UPDATE", "DELETE"}
# Tables where the app role gets less than full DML, by table; each migration that narrows a
# table's grants adds it here (and to 10-roles.sql's restore list). ``NO_APP_ACCESS`` tables get
# nothing at all.
RESTRICTED_APP_PRIVILEGES: dict[str, set[str]] = {
    # Migration 0004: the app role records and reads key check values, never changes or forgets
    # one (ADR 0004 section 3).
    "connection_keks": {"SELECT", "INSERT"},
    # Migration 0006 (#12): the app role reads the stored flags and holds nothing on the admin
    # audit log or its id sequence (the log is append-only by privileges).
    "app_settings": {"SELECT"},
    "admin_audit": set(),
    "admin_audit_id_seq": set(),
    # Migration 0009: column SELECT only, and no privilege on the identity sequence.
    "withings_apps": set(),
    "withings_apps_id_seq": set(),
}
# The only column-level privileges in public, exactly: (table, column) -> {(grantee, privilege)}.
# Maint's columns are guard 11's. The queue role reads three columns of ``connections`` and
# nothing else (20-queue.sql). ``withings_apps`` (#21): the app role reads these columns and
# cannot write the table. Webhook column grants are added when 40-withings.sql has run.
COLUMN_PRIVILEGES: dict[tuple[str, str], set[tuple[str, str]]] = {
    ("connections", "id"): {(QUEUE, "SELECT")},
    ("connections", "owner_id"): {(QUEUE, "SELECT")},
    ("connections", "status"): {(QUEUE, "SELECT")},
} | {("withings_apps", column): {(APP, "SELECT")} for column in APP_WITHINGS_COLUMNS}
# Table privileges of ``ownvitals_queue``, by ``relname:relkind``. Nothing else.
QUEUE_RELATIONS = {
    "jobs:r": {"SELECT", "INSERT", "UPDATE"},
    "job_events:r": {"SELECT", "INSERT"},
}
# ``regprocedure`` without a schema. #153 added the three beyond claim, reap and stats.
QUEUE_EXECUTE = {
    "queue_claim(text,integer,text[])",
    "queue_reap(integer)",
    "queue_stats()",
    "queue_enqueue(uuid,text,text,jsonb,text,integer,uuid)",
    "queue_cancel_for_secret(text,uuid)",
    "queue_replay(uuid)",
}
# 40-withings.sql: two NOLOGIN roles, table grants, and the owner of each function.
SECRETS = "ownvitals_webhook_secrets"
EVENTS = "ownvitals_webhook_events"
WITHINGS_ROLES = {SECRETS, EVENTS}
WITHINGS_RELATIONS: dict[str, dict[str, set[str]]] = {
    "admin_audit:r": {SECRETS: {"INSERT"}},
    "admin_audit_id_seq:S": {SECRETS: {"USAGE"}},
    "webhook_events:r": {EVENTS: {"INSERT"}},
}
WITHINGS_FUNCTIONS = {
    "ensure_month_partitions": OWNER,
    "webhook_secrets_apply": SECRETS,
    "webhook_event_insert": EVENTS,
}
# Column grants of ownvitals_webhook_secrets. Not maint, and not the client secret.
WITHINGS_COLUMNS: dict[tuple[str, str], set[tuple[str, str]]] = {
    ("withings_apps", "id"): {(SECRETS, "SELECT")},
    ("withings_apps", "webhook_secrets"): {(SECRETS, "SELECT"), (SECRETS, "UPDATE")},
    ("withings_apps", "rotation_requested_at"): {(SECRETS, "SELECT"), (SECRETS, "UPDATE")},
    ("withings_apps", "rotation_reason"): {(SECRETS, "SELECT"), (SECRETS, "UPDATE")},
    ("connection_keks", "key_id"): {(SECRETS, "SELECT")},
}
SEQUENCE_PRIVILEGES = {"USAGE", "SELECT"}
# Built-in languages that PUBLIC may use (Postgres' own default).
BUILTIN_LANGUAGES = {"plpgsql", "sql", "internal", "c"}
# The three exact timeouts, plus the roles script's ``transaction_timeout`` of 5 minutes.
EXPECTED_TIMEOUTS = {**EXPECTED_ROLE_SETTINGS, "transaction_timeout": "5min"}
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


def _bare_signature(obj: str) -> str:
    """``queue_claim(text,integer,text[]):owner:true`` to ``queue_claim(text,integer,text[])``."""
    signature = obj.split(":", 1)[0]
    name, sep, args = signature.partition("(")
    return name.rsplit(".", 1)[-1] + sep + args


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
    # Role settings: exactly the three timeouts and transaction_timeout of 5 minutes, at role
    # level. A per-database setting would take precedence over them, and anything else
    # (session_replication_role, search_path...) changes how the app role's sessions behave.
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
    # No default for an app.* setting anywhere (``app.user_id`` is the RLS user): not on a role,
    # a database or every role (``ALTER ROLE ALL``). Postgres keeps the name as written.
    app_defaults = conn.execute(
        text(
            "SELECT count(*) FROM pg_db_role_setting s, unnest(s.setconfig) c "
            "WHERE starts_with(lower(split_part(c, '=', 1)), 'app.')"
        )
    ).scalar_one()
    need(app_defaults == 0, "no role or database may have a default for an app.* setting")

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
    need(QUEUE not in _grantees(databases), "ownvitals_queue must hold nothing on the database")
    need(
        not (WITHINGS_ROLES & _grantees(databases)),
        "webhook roles must hold nothing on the database",
    )
    database_owner = conn.execute(
        text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")
    ).scalar_one()
    need(database_owner == OWNER, "ownvitals_owner must own the database")
    # The queue is installed with the migrated schema (db-functions). A database that has not
    # had the script applied yet has no queue role; the exact grants apply once it exists.
    # 40-withings.sql is the same: absent until that script runs, then exactly its grants.
    queue_installed = (
        conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :q"), {"q": QUEUE}).first()
        is not None
    )
    present_webhook_roles = {
        str(name)
        for (name,) in conn.execute(
            text("SELECT rolname FROM pg_roles WHERE rolname IN (:secrets, :events)"),
            {"secrets": SECRETS, "events": EVENTS},
        )
    }
    withings_installed = bool(present_webhook_roles)
    if withings_installed:
        need(
            present_webhook_roles == WITHINGS_ROLES,
            "ownvitals_webhook_secrets and ownvitals_webhook_events must exist together",
        )

    # Schemas.
    schemas = _acl(
        conn,
        "SELECT n.nspname, {G}, a.privilege_type, a.is_grantable FROM pg_namespace n, "
        "aclexplode(coalesce(n.nspacl, acldefault(CAST('n' AS \"char\"), n.nspowner))) a "
        "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'",
    )
    need(_privileges(schemas, APP, "public") == {"USAGE"}, "ownvitals_app: USAGE only on public")
    if queue_installed:
        need(
            _privileges(schemas, QUEUE, "public") == {"USAGE"},
            "ownvitals_queue: USAGE only on public",
        )
    else:
        need(QUEUE not in _grantees(schemas), "ownvitals_queue must not be a schema grantee")
    if withings_installed:
        for role in sorted(WITHINGS_ROLES):
            need(
                _privileges(schemas, role, "public") == {"USAGE"},
                f"{role}: USAGE only on public",
            )
    else:
        need(
            not (WITHINGS_ROLES & _grantees(schemas)),
            "webhook roles must not be schema grantees",
        )
    need(not _privileges(schemas, "PUBLIC", "public"), "PUBLIC must have no privilege on public")
    need(
        {"USAGE", "CREATE"} <= _privileges(schemas, OWNER, "public"),
        "ownvitals_owner needs USAGE and CREATE on public",
    )
    schema_grantees = GRANTEES | {QUEUE}
    if withings_installed:
        schema_grantees |= WITHINGS_ROLES
    need(_grantees(schemas) <= schema_grantees, "unexpected grantee on a schema")
    elsewhere = {APP, "PUBLIC", QUEUE} | WITHINGS_ROLES
    need(
        not [1 for o, g, _, _ in schemas if o != "public" and g in elsewhere],
        "no privilege for the app, PUBLIC, the queue or a webhook role on another schema",
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
        allowed = set(GRANTEES)
        if queue_installed and obj in QUEUE_RELATIONS:
            allowed.add(QUEUE)
            need(
                _privileges(relations, QUEUE, obj) == QUEUE_RELATIONS[obj],
                f"{obj}: ownvitals_queue privileges must be exactly {sorted(QUEUE_RELATIONS[obj])}",
            )
        else:
            need(
                QUEUE not in _grantees(relations, obj), f"{obj}: ownvitals_queue must hold nothing"
            )
        if withings_installed and obj in WITHINGS_RELATIONS:
            for role, privileges in WITHINGS_RELATIONS[obj].items():
                allowed.add(role)
                need(
                    _privileges(relations, role, obj) == privileges,
                    f"{obj}: {role} privileges must be exactly {sorted(privileges)}",
                )
        else:
            for role in WITHINGS_ROLES:
                need(role not in _grantees(relations, obj), f"{obj}: {role} must hold nothing")
        need(_grantees(relations, obj) <= allowed, f"{obj}: unexpected grantee")
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
        if grantee == MAINT:
            continue  # guard 11's allowlist
        found_columns.setdefault((str(table), str(column)), set()).add(
            (str(grantee), str(privilege))
        )
    present = {o.split(":")[0] for o, *_ in relations}
    expected_columns = {
        key: value
        for key, value in COLUMN_PRIVILEGES.items()
        if key[0] in present and (queue_installed or not any(g == QUEUE for g, _ in value))
    }
    if withings_installed:
        for key, extra in WITHINGS_COLUMNS.items():
            if key[0] in present:
                expected_columns[key] = set(expected_columns.get(key, ())) | extra
    need(
        found_columns == expected_columns,
        f"column-level privileges in public must be exactly {expected_columns}, "
        f"are {found_columns}",
    )

    # Functions (in public, and user-made ones anywhere): PUBLIC may call none, and ownvitals_app
    # may EXECUTE only the queue functions (ADR 0005 section 3) and, once 40-withings.sql has
    # run, the three functions that script creates. 16384 is FirstNormalObjectId: every function
    # a user made.
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
    signatures = {_bare_signature(obj) for obj, *_ in functions}
    if queue_installed:
        need(
            signatures >= QUEUE_EXECUTE,
            f"queue functions missing: {sorted(QUEUE_EXECUTE - signatures)}",
        )
    if withings_installed:
        found_names = {signature.partition("(")[0] for signature in signatures}
        missing = set(WITHINGS_FUNCTIONS) - found_names
        need(not missing, f"withings functions missing: {sorted(missing)}")
    for obj in sorted({o for o, *_ in functions}):
        signature = _bare_signature(obj)
        if queue_installed and signature in QUEUE_EXECUTE:
            need(
                _privileges(functions, APP, obj) == {"EXECUTE"},
                f"{obj}: ownvitals_app EXECUTE only",
            )
            need(
                _privileges(functions, QUEUE, obj) == {"EXECUTE"},
                f"{obj}: ownvitals_queue EXECUTE only",
            )
            need(_grantees(functions, obj) <= {APP, QUEUE}, f"{obj}: unexpected grantee")
            need(
                obj.endswith(f":{QUEUE}:true"),
                f"{obj}: must be owned by ownvitals_queue and be SECURITY DEFINER",
            )
            need(
                not any(grantable for o, _, _, grantable in functions if o == obj),
                f"{obj}: no grantable privilege",
            )
        elif withings_installed and signature.partition("(")[0] in WITHINGS_FUNCTIONS:
            owner = WITHINGS_FUNCTIONS[signature.partition("(")[0]]
            need(
                _privileges(functions, APP, obj) == {"EXECUTE"},
                f"{obj}: ownvitals_app EXECUTE only",
            )
            need(
                _privileges(functions, owner, obj) == {"EXECUTE"},
                f"{obj}: {owner} EXECUTE only",
            )
            need(_grantees(functions, obj) <= {APP, owner}, f"{obj}: unexpected grantee")
            need(
                obj.endswith(f":{owner}:true"),
                f"{obj}: must be owned by {owner} and be SECURITY DEFINER",
            )
            need(
                not any(grantable for o, _, _, grantable in functions if o == obj),
                f"{obj}: no grantable privilege",
            )
        else:
            need(
                not _privileges(functions, APP, obj),
                f"{obj}: ownvitals_app may execute no function",
            )
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
