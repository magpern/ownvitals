"""Guard 2 of ADR 0002 section 7: roles, privileges and defaults, checked as an allowlist.

``check_roles`` returns a list of violations (empty when the database matches section 5). It
reads ``pg_roles``, ``pg_auth_members``, ``pg_shdepend`` and the ACLs through ``aclexplode``, so
a privilege of a kind nobody thought of still fails.

``ownvitals_queue`` (ADR 0005 section 3) is an allowed grantee with exactly the privileges
``docker/postgres/queue/20-queue.sql`` grants, and ``ownvitals_app`` may ``EXECUTE`` the queue
functions. When ``docker/postgres/withings/40-withings.sql`` has run, the webhook NOLOGIN roles
and ``ensure_month_partitions`` hold exactly the grants that script creates, and the app role
may ``EXECUTE`` those functions as well and no other function; the metrics role
``ownvitals_metrics`` (#167) has exactly its attributes and column grants and owns the two
metrics aggregates. The admin role
``ownvitals_maint`` (#12) is an allowed grantee; guard 11 (``tests/test_maint_privileges.py``)
checks that it holds exactly its allowlist, so its column privileges are left out of the column
check here. Maint is not granted the client secret or ``webhook_secrets``. When
``docker/postgres/notify/50-outbox.sql`` has run (#194, ADR 0007 section 3), the NOLOGIN role
``ownvitals_mail_outbox`` has exactly its attributes and the grants that script creates, owns the
three outbox functions, and the app role may ``EXECUTE`` them; the app role holds nothing on
``owner_notices``. When ``docker/postgres/ai/50-ai.sql`` has run (#205, ADR 0014 section 7),
the NOLOGIN role ``ownvitals_ai`` has exactly its attributes and the column grants that script
creates, with ``DELETE`` on ``ai_audit`` for the retention purge (#331) as its only table
privilege, owns the five AI functions, and the app role may ``EXECUTE`` them. The app role
holds ``SELECT`` only on ``ai_spend`` and ``ai_reservations`` and nothing on ``ai_spend_global``.
When ``docker/postgres/exports/60-exports.sql`` has run (#333, ADR 0013 section 4), the NOLOGIN
role ``ownvitals_exports`` has exactly its attributes, the column grants that script creates (no
account table, no jobs) and
``DELETE`` on ``exports`` and ``export_chunks`` as its only table privileges, owns
``exports_purge_expired``, and the app role may ``EXECUTE`` it. When
``docker/postgres/maint/70-admin-accounts.sql`` has run (#314, ADR 0002 section 3), the NOLOGIN
role ``ownvitals_admin_accounts`` (no ``BYPASSRLS``) has exactly its attributes, the column grants
and the two ``DELETE`` privileges that script creates (no health table, no secret column), owns
the three admin account functions, which only ``ownvitals_maint`` may ``EXECUTE``, and is the
only grantee of ``webhook_events_purge_account``. The app role may execute none of those four.
Any other grantee fails.
"""

from collections.abc import Callable

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
    # Migration 0013 (#205, ADR 0014 section 7): the app role reads its budget and writes
    # nothing; every write is a definer function of 50-ai.sql. ai_spend_global is NO_APP_ACCESS.
    "ai_spend": {"SELECT"},
    "ai_reservations": {"SELECT"},
    # Migration 0023 (#207, ADR 0014 section 6): the AI audit log is append-only for the app.
    "ai_audit": {"SELECT", "INSERT"},
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
# ``regprocedure`` without a schema. #153 added the three beyond claim, reap and stats; #164
# added the scheduler's.
QUEUE_EXECUTE = {
    "queue_claim(text,integer,text[])",
    "queue_reap(integer)",
    "queue_stats()",
    "queue_enqueue(uuid,text,text,jsonb,text,integer,uuid)",
    "queue_cancel_for_secret(text,uuid)",
    "queue_replay(uuid)",
    "queue_schedule(text,integer)",
}
# 40-withings.sql: two NOLOGIN roles, table grants, and the owner of each function.
SECRETS = "ownvitals_webhook_secrets"
EVENTS = "ownvitals_webhook_events"
# #167: the metrics aggregates' owner. Read-only: column SELECT, BYPASSRLS, NOLOGIN.
METRICS = "ownvitals_metrics"
WITHINGS_ROLES = {SECRETS, EVENTS, METRICS}
# rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit
METRICS_ATTRIBUTES = (False, False, False, False, False, True, False)
# The webhook roles (#280): the secrets role has no BYPASSRLS (ADR 0002 section 3); the ingest
# role has it for the cross-owner connection lookup (ADR 0002 section 4). Both are NOLOGIN.
WEBHOOK_ATTRIBUTES = {
    SECRETS: ((False, False, False, False, False, False, False), "NOBYPASSRLS"),
    EVENTS: ((False, False, False, False, False, True, False), "BYPASSRLS"),
}
WITHINGS_RELATIONS: dict[str, dict[str, set[str]]] = {
    "admin_audit:r": {SECRETS: {"INSERT"}},
    "admin_audit_id_seq:S": {SECRETS: {"USAGE"}},
    "webhook_events:r": {EVENTS: {"INSERT", "DELETE"}},
}
# The tables ensure_month_partitions may partition (its p_table allowlist in 40-withings.sql).
MONTH_PARTITIONED = frozenset({"sleep_series", "intraday", "observations"})
WITHINGS_FUNCTIONS = {
    "ensure_month_partitions": OWNER,
    "webhook_secrets_apply": SECRETS,
    "webhook_event_insert": EVENTS,
    "webhook_events_purge_owner": EVENTS,
    "webhook_events_purge_account": EVENTS,
    "metrics_withings_aggregates": METRICS,
    "metrics_webhook_ages": METRICS,
}
# rate.sql (#23): the app role may EXECUTE these and no other new function. Same owner.
RATE_FUNCTIONS = {
    "withings_rate_claim": OWNER,
    "withings_rate_release": OWNER,
    "withings_rate_pause": OWNER,
}
# 50-outbox.sql (#194): the outbox family, owned by its own NOLOGIN role.
OUTBOX = "ownvitals_mail_outbox"
OUTBOX_FUNCTIONS = {
    "mail_outbox_claim": OUTBOX,
    "mail_outbox_mark": OUTBOX,
    "owner_notice_queue": OUTBOX,
}
# Table-level privileges of the outbox role (its reads and writes are column grants, below).
OUTBOX_RELATIONS = {
    "mail_outbox:r": {"DELETE"},
    "owner_notices:r": {"DELETE"},
}
_OUTBOX_READ = (
    "id",
    "kind",
    "route",
    "state",
    "attempts",
    "hold_until",
    "created_at",
    "updated_at",
)
_OUTBOX_WRITE = ("state", "attempts", "hold_until", "updated_at", "sent_at")
OUTBOX_COLUMNS: dict[tuple[str, str], set[tuple[str, str]]] = {}
for _table in ("mail_outbox", "owner_notices"):
    for _column in {*_OUTBOX_READ, *_OUTBOX_WRITE}:
        OUTBOX_COLUMNS[(_table, _column)] = {
            *({(OUTBOX, "SELECT")} if _column in _OUTBOX_READ else set()),
            *({(OUTBOX, "UPDATE")} if _column in _OUTBOX_WRITE else set()),
        }
OUTBOX_COLUMNS[("mail_outbox", "owner_id")] = {(OUTBOX, "SELECT")}
# The owner-notice write inserts a kind and its route, nothing else.
OUTBOX_COLUMNS[("owner_notices", "kind")] |= {(OUTBOX, "INSERT")}
OUTBOX_COLUMNS[("owner_notices", "route")] |= {(OUTBOX, "INSERT")}
# rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit
OUTBOX_ATTRIBUTES = (False, False, False, False, False, True, False)
# 50-ai.sql (#205): the AI cost cap's family, owned by its own NOLOGIN role.
AI = "ownvitals_ai"
AI_FUNCTIONS = {
    "ai_reserve": AI,
    "ai_settle": AI,
    "ai_release": AI,
    "ai_settle_lost": AI,
    "ai_audit_purge": AI,
}
# Its one table-level privilege: DELETE on ai_audit, for the 12-month retention purge (ADR 0014
# section 6; #331). Everything else is a column grant (ADR 0014 section 7).
AI_RELATIONS = {"ai_audit:r": {"DELETE"}}
# Its column grants: the budget functions' reads and writes, and the purge's id and age reads.
AI_COLUMNS: dict[tuple[str, str], set[tuple[str, str]]] = {
    ("jobs", "id"): {(AI, "SELECT")},
    ("jobs", "owner_id"): {(AI, "SELECT")},
    ("jobs", "state"): {(AI, "SELECT")},
    ("jobs", "lease_version"): {(AI, "SELECT")},
    ("jobs", "lease_expires_at"): {(AI, "SELECT")},
    ("ai_spend_global", "month"): {(AI, "SELECT"), (AI, "INSERT")},
    ("ai_spend_global", "reserved"): {(AI, "SELECT"), (AI, "UPDATE")},
    ("ai_spend_global", "spent"): {(AI, "SELECT"), (AI, "UPDATE")},
    ("ai_spend_global", "updated_at"): {(AI, "UPDATE")},
    ("ai_spend", "owner_id"): {(AI, "SELECT"), (AI, "INSERT")},
    ("ai_spend", "month"): {(AI, "SELECT"), (AI, "INSERT")},
    ("ai_spend", "reserved"): {(AI, "SELECT"), (AI, "UPDATE")},
    ("ai_spend", "spent"): {(AI, "SELECT"), (AI, "UPDATE")},
    ("ai_spend", "updated_at"): {(AI, "UPDATE")},
    ("ai_reservations", "id"): {(AI, "SELECT")},
    ("ai_reservations", "owner_id"): {(AI, "SELECT"), (AI, "INSERT")},
    ("ai_reservations", "month"): {(AI, "SELECT"), (AI, "INSERT")},
    ("ai_reservations", "amount"): {(AI, "SELECT"), (AI, "INSERT")},
    ("ai_reservations", "state"): {(AI, "SELECT"), (AI, "UPDATE")},
    ("ai_reservations", "job_id"): {(AI, "SELECT"), (AI, "INSERT")},
    ("ai_reservations", "lease_version"): {(AI, "SELECT"), (AI, "INSERT")},
    ("ai_reservations", "actual"): {(AI, "UPDATE")},
    ("ai_reservations", "ended_at"): {(AI, "UPDATE")},
    ("ai_audit", "id"): {(AI, "SELECT")},
    ("ai_audit", "created_at"): {(AI, "SELECT")},
}
# rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit
AI_ATTRIBUTES = (False, False, False, False, False, True, False)
# 60-exports.sql (#333): the export purge, owned by its own NOLOGIN role.
EXPORTS = "ownvitals_exports"
EXPORTS_FUNCTIONS = {"exports_purge_expired": EXPORTS}
# Its table-level privileges: DELETE of exports past their 90 days and of spent chunks.
EXPORTS_RELATIONS = {"exports:r": {"DELETE"}, "export_chunks:r": {"DELETE"}}
# Its column grants: what the purge reads and changes of exports and the join of a chunk to its
# export (ADR 0013 section 4: counts only, no audit row, no account table, no jobs).
EXPORTS_COLUMNS: dict[tuple[str, str], set[tuple[str, str]]] = {
    ("exports", "id"): {(EXPORTS, "SELECT")},
    ("exports", "owner_id"): {(EXPORTS, "SELECT")},
    ("exports", "state"): {(EXPORTS, "SELECT"), (EXPORTS, "UPDATE")},
    ("exports", "created_at"): {(EXPORTS, "SELECT")},
    ("exports", "expires_at"): {(EXPORTS, "SELECT")},
    ("export_chunks", "id"): {(EXPORTS, "SELECT")},
    ("export_chunks", "owner_id"): {(EXPORTS, "SELECT")},
    ("export_chunks", "export_id"): {(EXPORTS, "SELECT")},
    ("export_chunks", "seq"): {(EXPORTS, "SELECT")},
}
EXPORTS_ATTRIBUTES = (False, False, False, False, False, True, False)
# 70-admin-accounts.sql (#314): the admin account family, owned by its own NOLOGIN role and
# executable by maint alone. No BYPASSRLS: it touches system tables only.
ADMIN_ACCOUNTS = "ownvitals_admin_accounts"
ADMIN_ACCOUNTS_FUNCTIONS = {
    "admin_account_disable": ADMIN_ACCOUNTS,
    "admin_account_enable": ADMIN_ACCOUNTS,
    "admin_account_delete": ADMIN_ACCOUNTS,
}
ADMIN_ACCOUNTS_ATTRIBUTES = (False, False, False, False, False, False, False)
# Its table-level privileges: the account delete and the disable's sign-out everywhere.
ADMIN_ACCOUNTS_RELATIONS = {"users:r": {"DELETE"}, "sessions:r": {"DELETE"}}
# Its column grants (ADR 0002 section 3): never password_hash, a session token, or the audit
# row's IP HMAC, and no write to the audit log.
ADMIN_ACCOUNTS_COLUMNS: dict[tuple[str, str], set[tuple[str, str]]] = {
    ("users", "id"): {(ADMIN_ACCOUNTS, "SELECT")},
    ("users", "role"): {(ADMIN_ACCOUNTS, "SELECT")},
    ("users", "email_normalized"): {(ADMIN_ACCOUNTS, "SELECT")},
    ("users", "disabled_at"): {(ADMIN_ACCOUNTS, "SELECT"), (ADMIN_ACCOUNTS, "UPDATE")},
    ("sessions", "user_id"): {(ADMIN_ACCOUNTS, "SELECT")},
    **{
        ("auth_events", c): {(ADMIN_ACCOUNTS, "INSERT")}
        for c in ("kind", "ip", "browser", "os", "place")
    },
    **{
        ("admin_audit", c): {(ADMIN_ACCOUNTS, "SELECT")}
        for c in ("xmin", "created_at", "actor_id", "action", "target_user_id", "outcome")
    },
}
# The admin delete's purge (#362): its owner and, once the family is installed, that role alone.
PURGE_ACCOUNT = "webhook_events_purge_account"
_CALLABLE = {
    **WITHINGS_FUNCTIONS,
    **RATE_FUNCTIONS,
    **OUTBOX_FUNCTIONS,
    **AI_FUNCTIONS,
    **EXPORTS_FUNCTIONS,
}
# Column grants of ownvitals_webhook_secrets. Not maint, and not the client secret.
WITHINGS_COLUMNS: dict[tuple[str, str], set[tuple[str, str]]] = {
    ("withings_apps", "id"): {(SECRETS, "SELECT")},
    ("withings_apps", "webhook_secrets"): {(SECRETS, "SELECT"), (SECRETS, "UPDATE")},
    ("withings_apps", "rotation_requested_at"): {(SECRETS, "SELECT"), (SECRETS, "UPDATE")},
    ("withings_apps", "rotation_reason"): {(SECRETS, "SELECT"), (SECRETS, "UPDATE")},
    ("connection_keks", "key_id"): {(SECRETS, "SELECT")},
    # Ingest resolves a notification to a connection (ADR 0002 section 4). No credential column.
    ("connections", "id"): {(EVENTS, "SELECT")},
    ("connections", "app_id"): {(EVENTS, "SELECT")},
    ("connections", "withings_userid"): {(EVENTS, "SELECT")},
    ("connections", "status"): {(EVENTS, "SELECT")},
    # Account deletion matches notification rows to this owner (ADR 0002 section 3).
    ("connections", "owner_id"): {(EVENTS, "SELECT")},
    ("webhook_events", "app_id"): {(EVENTS, "SELECT"), (METRICS, "SELECT")},
    ("webhook_events", "withings_userid"): {(EVENTS, "SELECT"), (METRICS, "SELECT")},
    # The webhook quotas count the job-bearing events of the last hour (#280).
    ("webhook_events", "job_id"): {(EVENTS, "SELECT")},
    # The metrics aggregates (#167): counts and ages. No credential, owner or payload column.
    ("connections", "public"): {(METRICS, "SELECT")},
    ("sync_cursors", "connection_id"): {(METRICS, "SELECT")},
    ("sync_cursors", "stream"): {(METRICS, "SELECT")},
    ("sync_cursors", "capability"): {(METRICS, "SELECT")},
    ("sync_cursors", "backfill_state"): {(METRICS, "SELECT")},
    ("sync_cursors", "last_ok_at"): {(METRICS, "SELECT")},
    ("webhook_events", "enddate"): {(METRICS, "SELECT")},
    # The tie-break of the delivery-age watermark (received_at, id) (#303).
    ("webhook_events", "id"): {(METRICS, "SELECT")},
    ("webhook_events", "received_at"): {(EVENTS, "SELECT"), (METRICS, "SELECT")},
    ("jobs", "state"): {(METRICS, "SELECT")},
    ("jobs", "available_at"): {(METRICS, "SELECT")},
}
for _column in ("id", "app_id", "withings_userid", "status"):
    WITHINGS_COLUMNS[("connections", _column)] |= {(METRICS, "SELECT")}
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


def _check_webhook_roles(conn: Connection, need: Callable[[bool, str], None]) -> None:
    """40-withings.sql's two webhook roles: exactly their attributes, a member of nothing and
    with no members, no settings and no default privileges (#280)."""
    for name, (attributes, bypass) in WEBHOOK_ATTRIBUTES.items():
        row = conn.execute(
            text(
                "SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, "
                "r.rolreplication, r.rolbypassrls, r.rolinherit, r.oid "
                "FROM pg_roles r WHERE r.rolname = :r"
            ),
            {"r": name},
        ).first()
        if row is None:
            continue  # reported as "must exist together"
        need(
            tuple(row[:7]) == attributes,
            f"{name} must be NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            f"{bypass} NOINHERIT",
        )
        others = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM pg_auth_members WHERE :oid IN (member, roleid)) "
                "+ (SELECT count(*) FROM pg_db_role_setting WHERE setrole = :oid) "
                "+ (SELECT count(*) FROM pg_default_acl WHERE defaclrole = :oid)"
            ),
            {"oid": row.oid},
        ).scalar_one()
        need(others == 0, f"{name}: no memberships, settings or default privileges")


def _check_metrics_role(conn: Connection, need: Callable[[bool, str], None]) -> None:
    """40-withings.sql's ownvitals_metrics (#167): exactly its attributes, a member of nothing,
    no settings or default privileges, and the owner of exactly the two aggregates."""
    row = conn.execute(
        text(
            "SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, "
            "r.rolbypassrls, r.rolinherit, r.oid FROM pg_roles r WHERE r.rolname = :m"
        ),
        {"m": METRICS},
    ).one()
    need(
        tuple(row[:7]) == METRICS_ATTRIBUTES,
        f"{METRICS} must be NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
        "BYPASSRLS NOINHERIT",
    )
    others = conn.execute(
        text(
            "SELECT (SELECT count(*) FROM pg_auth_members WHERE :oid IN (member, roleid)) "
            "+ (SELECT count(*) FROM pg_db_role_setting WHERE setrole = :oid) "
            "+ (SELECT count(*) FROM pg_default_acl WHERE defaclrole = :oid)"
        ),
        {"oid": row.oid},
    ).scalar_one()
    need(others == 0, f"{METRICS}: no memberships, settings or default privileges")
    owned = conn.execute(
        text(
            "SELECT coalesce(p.proname, s.classid::regclass::text) FROM pg_shdepend s "
            "LEFT JOIN pg_proc p ON p.oid = s.objid AND s.classid = CAST('pg_proc' AS regclass) "
            "WHERE s.refobjid = :oid AND s.deptype = 'o'"
        ),
        {"oid": row.oid},
    ).scalars()
    expected = sorted(n for n, owner in WITHINGS_FUNCTIONS.items() if owner == METRICS)
    found = sorted(str(name) for name in owned)
    need(found == expected, f"{METRICS} must own exactly {expected}, owns {found}")


def _definer_role(
    need: Callable[[bool, str], None],
    conn: Connection,
    role: str,
    attributes: tuple[bool, ...],
    functions: dict[str, str],
) -> bool:
    """Whether the NOLOGIN definer ``role`` exists; if it does, that it has exactly
    ``attributes``, no memberships either way, no settings, no default privileges, and owns
    exactly ``functions`` (the pattern of the AI role's checks above, for the export purge)."""
    row = conn.execute(
        text(
            "SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, "
            "r.rolbypassrls, r.rolinherit, r.oid FROM pg_roles r WHERE r.rolname = :a"
        ),
        {"a": role},
    ).first()
    if row is None:
        return False
    need(
        tuple(row[:7]) == attributes,
        f"{role} must be NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
        "BYPASSRLS NOINHERIT",
    )
    memberships = conn.execute(
        text("SELECT count(*) FROM pg_auth_members WHERE :oid IN (member, roleid)"),
        {"oid": row.oid},
    ).scalar_one()
    need(memberships == 0, f"{role} must be a member of no role and have no members")
    settings = conn.execute(
        text(
            "SELECT (SELECT count(*) FROM pg_db_role_setting WHERE setrole = :oid) "
            "+ (SELECT count(*) FROM pg_default_acl WHERE defaclrole = :oid)"
        ),
        {"oid": row.oid},
    ).scalar_one()
    need(settings == 0, f"{role} must have no settings and no default privileges")
    owned = (
        conn.execute(
            text(
                "SELECT p.proname FROM pg_shdepend s JOIN pg_proc p ON p.oid = s.objid "
                "WHERE s.refobjid = :oid AND s.deptype = 'o' "
                "AND s.classid = CAST('pg_proc' AS regclass) "
                "UNION ALL SELECT s.classid::regclass::text FROM pg_shdepend s "
                "WHERE s.refobjid = :oid AND s.deptype = 'o' "
                "AND s.classid <> CAST('pg_proc' AS regclass)"
            ),
            {"oid": row.oid},
        )
        .scalars()
        .all()
    )
    need(
        sorted(owned) == sorted(functions),
        f"{role} must own exactly {sorted(functions)}, owns {sorted(owned)}",
    )
    return True


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
    need(OUTBOX not in _grantees(databases), f"{OUTBOX} must hold nothing on the database")
    need(AI not in _grantees(databases), f"{AI} must hold nothing on the database")
    need(EXPORTS not in _grantees(databases), f"{EXPORTS} must hold nothing on the database")
    need(
        ADMIN_ACCOUNTS not in _grantees(databases),
        f"{ADMIN_ACCOUNTS} must hold nothing on the database",
    )
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
            text("SELECT rolname FROM pg_roles WHERE rolname IN (:secrets, :events, :metrics)"),
            {"secrets": SECRETS, "events": EVENTS, "metrics": METRICS},
        )
    }
    withings_installed = bool(present_webhook_roles)
    if withings_installed:
        need(
            present_webhook_roles == WITHINGS_ROLES,
            f"{SECRETS}, {EVENTS} and {METRICS} must exist together",
        )
        _check_metrics_role(conn, need)
        _check_webhook_roles(conn, need)
    # 50-outbox.sql: absent until that script runs, then exactly its role and grants.
    outbox_role = conn.execute(
        text(
            "SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, "
            "r.rolbypassrls, r.rolinherit, r.oid FROM pg_roles r WHERE r.rolname = :o"
        ),
        {"o": OUTBOX},
    ).first()
    outbox_installed = outbox_role is not None
    if outbox_role is not None:
        need(
            tuple(outbox_role[:7]) == OUTBOX_ATTRIBUTES,
            f"{OUTBOX} must be NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "BYPASSRLS NOINHERIT",
        )
        outbox_memberships = conn.execute(
            text("SELECT count(*) FROM pg_auth_members WHERE :oid IN (member, roleid)"),
            {"oid": outbox_role.oid},
        ).scalar_one()
        need(outbox_memberships == 0, f"{OUTBOX} must be a member of no role and have no members")
        outbox_settings = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM pg_db_role_setting WHERE setrole = :oid) "
                "+ (SELECT count(*) FROM pg_default_acl WHERE defaclrole = :oid)"
            ),
            {"oid": outbox_role.oid},
        ).scalar_one()
        need(outbox_settings == 0, f"{OUTBOX} must have no settings and no default privileges")
        outbox_owned = (
            conn.execute(
                text(
                    "SELECT p.proname FROM pg_shdepend s JOIN pg_proc p ON p.oid = s.objid "
                    "WHERE s.refobjid = :oid AND s.deptype = 'o' "
                    "AND s.classid = CAST('pg_proc' AS regclass) "
                    "UNION ALL SELECT s.classid::regclass::text FROM pg_shdepend s "
                    "WHERE s.refobjid = :oid AND s.deptype = 'o' "
                    "AND s.classid <> CAST('pg_proc' AS regclass)"
                ),
                {"oid": outbox_role.oid},
            )
            .scalars()
            .all()
        )
        need(
            sorted(outbox_owned) == sorted(OUTBOX_FUNCTIONS),
            f"{OUTBOX} must own exactly the outbox functions, owns {sorted(outbox_owned)}",
        )
    # 50-ai.sql: absent until that script runs, then exactly its role and grants.
    ai_role = conn.execute(
        text(
            "SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, "
            "r.rolbypassrls, r.rolinherit, r.oid FROM pg_roles r WHERE r.rolname = :a"
        ),
        {"a": AI},
    ).first()
    ai_installed = ai_role is not None
    if ai_role is not None:
        need(
            tuple(ai_role[:7]) == AI_ATTRIBUTES,
            f"{AI} must be NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "BYPASSRLS NOINHERIT",
        )
        ai_memberships = conn.execute(
            text("SELECT count(*) FROM pg_auth_members WHERE :oid IN (member, roleid)"),
            {"oid": ai_role.oid},
        ).scalar_one()
        need(ai_memberships == 0, f"{AI} must be a member of no role and have no members")
        ai_settings = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM pg_db_role_setting WHERE setrole = :oid) "
                "+ (SELECT count(*) FROM pg_default_acl WHERE defaclrole = :oid)"
            ),
            {"oid": ai_role.oid},
        ).scalar_one()
        need(ai_settings == 0, f"{AI} must have no settings and no default privileges")
        ai_owned = (
            conn.execute(
                text(
                    "SELECT p.proname FROM pg_shdepend s JOIN pg_proc p ON p.oid = s.objid "
                    "WHERE s.refobjid = :oid AND s.deptype = 'o' "
                    "AND s.classid = CAST('pg_proc' AS regclass) "
                    "UNION ALL SELECT s.classid::regclass::text FROM pg_shdepend s "
                    "WHERE s.refobjid = :oid AND s.deptype = 'o' "
                    "AND s.classid <> CAST('pg_proc' AS regclass)"
                ),
                {"oid": ai_role.oid},
            )
            .scalars()
            .all()
        )
        need(
            sorted(ai_owned) == sorted(AI_FUNCTIONS),
            f"{AI} must own exactly the AI functions, owns {sorted(ai_owned)}",
        )
    # 60-exports.sql: absent until that script runs, then exactly its role and grants.
    exports_installed = _definer_role(need, conn, EXPORTS, EXPORTS_ATTRIBUTES, EXPORTS_FUNCTIONS)
    # 70-admin-accounts.sql: absent until that script runs, then exactly its role and grants.
    admin_installed = _definer_role(
        need, conn, ADMIN_ACCOUNTS, ADMIN_ACCOUNTS_ATTRIBUTES, ADMIN_ACCOUNTS_FUNCTIONS
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
    if outbox_installed:
        need(_privileges(schemas, OUTBOX, "public") == {"USAGE"}, f"{OUTBOX}: USAGE only on public")
    else:
        need(OUTBOX not in _grantees(schemas), f"{OUTBOX} must not be a schema grantee")
    if ai_installed:
        need(_privileges(schemas, AI, "public") == {"USAGE"}, f"{AI}: USAGE only on public")
    else:
        need(AI not in _grantees(schemas), f"{AI} must not be a schema grantee")
    if exports_installed:
        need(
            _privileges(schemas, EXPORTS, "public") == {"USAGE"},
            f"{EXPORTS}: USAGE only on public",
        )
    else:
        need(EXPORTS not in _grantees(schemas), f"{EXPORTS} must not be a schema grantee")
    if admin_installed:
        need(
            _privileges(schemas, ADMIN_ACCOUNTS, "public") == {"USAGE"},
            f"{ADMIN_ACCOUNTS}: USAGE only on public",
        )
    else:
        need(
            ADMIN_ACCOUNTS not in _grantees(schemas),
            f"{ADMIN_ACCOUNTS} must not be a schema grantee",
        )
    need(not _privileges(schemas, "PUBLIC", "public"), "PUBLIC must have no privilege on public")
    need(
        {"USAGE", "CREATE"} <= _privileges(schemas, OWNER, "public"),
        "ownvitals_owner needs USAGE and CREATE on public",
    )
    schema_grantees = GRANTEES | {QUEUE}
    if withings_installed:
        schema_grantees |= WITHINGS_ROLES
    if outbox_installed:
        schema_grantees |= {OUTBOX}
    if ai_installed:
        schema_grantees |= {AI}
    if exports_installed:
        schema_grantees |= {EXPORTS}
    if admin_installed:
        schema_grantees |= {ADMIN_ACCOUNTS}
    need(_grantees(schemas) <= schema_grantees, "unexpected grantee on a schema")
    elsewhere = {APP, "PUBLIC", QUEUE, OUTBOX, AI, EXPORTS, ADMIN_ACCOUNTS} | WITHINGS_ROLES
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
    # Month partitions of ensure_month_partitions' tables (40-withings.sql): that function
    # revokes every privilege of the app role and PUBLIC on each one, so the app role reaches
    # the rows only through the parent, under its policy (#280).
    month_partitions = {
        f"{name}:r"
        for (name,) in conn.execute(
            text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_inherits i ON i.inhrelid = c.oid "
                "JOIN pg_class p ON p.oid = i.inhparent "
                "WHERE n.nspname = 'public' AND c.relispartition AND c.relkind = 'r' "
                "AND p.relname = ANY(:parents) AND p.relkind = 'p'"
            ),
            {"parents": sorted(MONTH_PARTITIONED)},
        )
    }
    for obj in sorted({o for o, *_ in relations}):
        expected = SEQUENCE_PRIVILEGES if obj.endswith(":S") else TABLE_PRIVILEGES
        expected = RESTRICTED_APP_PRIVILEGES.get(obj.split(":")[0], expected)
        if obj.split(":")[0] in NO_APP_ACCESS or obj in month_partitions:
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
        if outbox_installed and obj in OUTBOX_RELATIONS:
            allowed.add(OUTBOX)
            need(
                _privileges(relations, OUTBOX, obj) == OUTBOX_RELATIONS[obj],
                f"{obj}: {OUTBOX} privileges must be exactly {sorted(OUTBOX_RELATIONS[obj])}",
            )
        else:
            need(OUTBOX not in _grantees(relations, obj), f"{obj}: {OUTBOX} must hold nothing")
        # The AI role holds column grants (checked below) and DELETE on ai_audit, nothing else.
        if ai_installed and obj in AI_RELATIONS:
            allowed.add(AI)
            need(
                _privileges(relations, AI, obj) == AI_RELATIONS[obj],
                f"{obj}: {AI} privileges must be exactly {sorted(AI_RELATIONS[obj])}",
            )
        else:
            need(AI not in _grantees(relations, obj), f"{obj}: {AI} must hold no table privilege")
        # The export purge's role: column grants (below) and DELETE on its two tables.
        if exports_installed and obj in EXPORTS_RELATIONS:
            allowed.add(EXPORTS)
            need(
                _privileges(relations, EXPORTS, obj) == EXPORTS_RELATIONS[obj],
                f"{obj}: {EXPORTS} privileges must be exactly {sorted(EXPORTS_RELATIONS[obj])}",
            )
        else:
            need(
                EXPORTS not in _grantees(relations, obj),
                f"{obj}: {EXPORTS} must hold no table privilege",
            )
        # The admin account role: column grants (below) and DELETE on users and sessions.
        if admin_installed and obj in ADMIN_ACCOUNTS_RELATIONS:
            allowed.add(ADMIN_ACCOUNTS)
            need(
                _privileges(relations, ADMIN_ACCOUNTS, obj) == ADMIN_ACCOUNTS_RELATIONS[obj],
                f"{obj}: {ADMIN_ACCOUNTS} privileges must be exactly "
                f"{sorted(ADMIN_ACCOUNTS_RELATIONS[obj])}",
            )
        else:
            need(
                ADMIN_ACCOUNTS not in _grantees(relations, obj),
                f"{obj}: {ADMIN_ACCOUNTS} must hold no table privilege",
            )
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
    if outbox_installed:
        for key, extra in OUTBOX_COLUMNS.items():
            if key[0] in present:
                expected_columns[key] = set(expected_columns.get(key, ())) | extra
    if ai_installed:
        for key, extra in AI_COLUMNS.items():
            if key[0] in present:
                expected_columns[key] = set(expected_columns.get(key, ())) | extra
    if exports_installed:
        for key, extra in EXPORTS_COLUMNS.items():
            if key[0] in present:
                expected_columns[key] = set(expected_columns.get(key, ())) | extra
    if admin_installed:
        for key, extra in ADMIN_ACCOUNTS_COLUMNS.items():
            if key[0] in present:
                expected_columns[key] = set(expected_columns.get(key, ())) | extra
    need(
        found_columns == expected_columns,
        f"column-level privileges in public must be exactly {expected_columns}, "
        f"are {found_columns}",
    )

    # Functions (in public, and user-made ones anywhere): PUBLIC may call none, and ownvitals_app
    # may EXECUTE only the queue functions (ADR 0005 section 3) and, once 40-withings.sql has
    # run, the functions that script creates. 16384 is FirstNormalObjectId: every function
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
    if outbox_installed:
        found_names = {signature.partition("(")[0] for signature in signatures}
        missing = set(OUTBOX_FUNCTIONS) - found_names
        need(not missing, f"outbox functions missing: {sorted(missing)}")
    if ai_installed:
        found_names = {signature.partition("(")[0] for signature in signatures}
        missing = set(AI_FUNCTIONS) - found_names
        need(not missing, f"AI budget functions missing: {sorted(missing)}")
    if exports_installed:
        found_names = {signature.partition("(")[0] for signature in signatures}
        missing = set(EXPORTS_FUNCTIONS) - found_names
        need(not missing, f"export purge missing: {sorted(missing)}")
    if admin_installed:
        found_names = {signature.partition("(")[0] for signature in signatures}
        missing = set(ADMIN_ACCOUNTS_FUNCTIONS) - found_names
        need(not missing, f"admin account functions missing: {sorted(missing)}")
    for obj in sorted({o for o, *_ in functions}):
        signature = _bare_signature(obj)
        if queue_installed and signature in QUEUE_EXECUTE:
            allowed_execute = {APP, QUEUE}
            if signature.startswith("queue_enqueue("):
                # The ingest function calls it in the notification's transaction.
                allowed_execute.add(EVENTS)
            need(
                _privileges(functions, APP, obj) == {"EXECUTE"},
                f"{obj}: ownvitals_app EXECUTE only",
            )
            need(
                _privileges(functions, QUEUE, obj) == {"EXECUTE"},
                f"{obj}: ownvitals_queue EXECUTE only",
            )
            if signature.startswith("queue_enqueue(") and withings_installed:
                # Required, not just allowed: without it every notification fails (#280).
                need(
                    _privileges(functions, EVENTS, obj) == {"EXECUTE"},
                    f"{obj}: {EVENTS} must have EXECUTE and nothing else",
                )
            else:
                need(EVENTS not in _grantees(functions, obj), f"{obj}: {EVENTS} must hold nothing")
            need(_grantees(functions, obj) <= allowed_execute, f"{obj}: unexpected grantee")
            need(
                obj.endswith(f":{QUEUE}:true"),
                f"{obj}: must be owned by ownvitals_queue and be SECURITY DEFINER",
            )
            need(
                not any(grantable for o, _, _, grantable in functions if o == obj),
                f"{obj}: no grantable privilege",
            )
        elif signature.partition("(")[0] in ADMIN_ACCOUNTS_FUNCTIONS and admin_installed:
            # Maint's alone (#314); guard 11 checks that maint holds exactly these.
            need(not _privileges(functions, APP, obj), f"{obj}: ownvitals_app may not execute it")
            need(
                _privileges(functions, ADMIN_ACCOUNTS, obj) == {"EXECUTE"},
                f"{obj}: {ADMIN_ACCOUNTS} EXECUTE only",
            )
            need(
                _privileges(functions, MAINT, obj) == {"EXECUTE"},
                f"{obj}: {MAINT} must have EXECUTE and nothing else",
            )
            need(_grantees(functions, obj) <= {ADMIN_ACCOUNTS, MAINT}, f"{obj}: unexpected grantee")
            need(
                obj.endswith(f":{ADMIN_ACCOUNTS}:true"),
                f"{obj}: must be owned by {ADMIN_ACCOUNTS} and be SECURITY DEFINER",
            )
            need(
                not any(grantable for o, _, _, grantable in functions if o == obj),
                f"{obj}: no grantable privilege",
            )
        elif signature.partition("(")[0] == PURGE_ACCOUNT and withings_installed:
            # No runtime role calls it (#314): the admin account delete, once installed, alone.
            need(not _privileges(functions, APP, obj), f"{obj}: ownvitals_app may not execute it")
            need(
                _privileges(functions, EVENTS, obj) == {"EXECUTE"}, f"{obj}: {EVENTS} EXECUTE only"
            )
            callers = {EVENTS, ADMIN_ACCOUNTS} if admin_installed else {EVENTS}
            need(_grantees(functions, obj) == callers, f"{obj}: grantees must be {sorted(callers)}")
            if admin_installed:
                need(
                    _privileges(functions, ADMIN_ACCOUNTS, obj) == {"EXECUTE"},
                    f"{obj}: {ADMIN_ACCOUNTS} must have EXECUTE and nothing else",
                )
            need(
                obj.endswith(f":{EVENTS}:true"),
                f"{obj}: must be owned by {EVENTS} and be SECURITY DEFINER",
            )
            need(
                not any(grantable for o, _, _, grantable in functions if o == obj),
                f"{obj}: no grantable privilege",
            )
        elif signature.partition("(")[0] in _CALLABLE and (
            signature.partition("(")[0] in RATE_FUNCTIONS
            or (signature.partition("(")[0] in WITHINGS_FUNCTIONS and withings_installed)
            or (signature.partition("(")[0] in OUTBOX_FUNCTIONS and outbox_installed)
            or (signature.partition("(")[0] in AI_FUNCTIONS and ai_installed)
            or (signature.partition("(")[0] in EXPORTS_FUNCTIONS and exports_installed)
        ):
            owner = _CALLABLE[signature.partition("(")[0]]
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
