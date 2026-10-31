"""Guard 1 of ADR 0002 section 7: the contract every table in ``public`` must meet.

``check_tables`` returns a list of violations (empty when the schema is fine). It reads the
catalog only, so it checks the real schema, whatever created it.

Partitioned tables are refused, owned or not, until #21 extends this guard to the partition rules
of ADR 0002 section 2 (roadmap Amendment 2).
"""

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

# What Postgres 17 stores for the policy of ownvitals.db.rls (pg_policies renders it).
EXPECTED_EXPRESSION = (
    "(owner_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)"
)
# Restrictive ``USING (false)`` policies an owned table must have, by table: ``{"UPDATE",
# "DELETE"}`` for an append-only table (ADR 0002 section 2). The job events (ADR 0005 section 1)
# are append-only; they go only with their job (``ON DELETE CASCADE``).
REQUIRED_DENY_POLICIES: dict[str, set[str]] = {"job_events": {"UPDATE", "DELETE"}}
# Restrictive policies with their own expressions, by table: ``(cmd, USING, WITH CHECK)`` as
# ``pg_policies`` renders them.
REQUIRED_POLICIES: dict[str, set[tuple[str, str | None, str | None]]] = {}
# The one sanctioned owner-less unique index (ADR 0002 section 2): index name -> (table, key
# columns). #22 registers ``connections (app_id, withings_userid)`` here when it creates it;
# any other unique index on an owned table must include owner_id.
GLOBAL_UNIQUE_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {}
SAFE_DELETE_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE"}
SET_ACTIONS = {"n": "SET NULL", "d": "SET DEFAULT"}


def _rows(conn: Connection, sql: str) -> list[Any]:
    return list(conn.execute(text(sql)).mappings().all())


def check_tables(
    conn: Connection,
    system_tables: Mapping[str, str],
    global_unique: Mapping[str, tuple[str, tuple[str, ...]]] = GLOBAL_UNIQUE_INDEXES,
) -> list[str]:
    problems: list[str] = []
    relations = _rows(
        conn,
        """SELECT c.oid, c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,
                  c.relispartition
             FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')""",
    )
    by_oid = {r["oid"]: r for r in relations}
    columns: dict[int, dict[int, Any]] = defaultdict(dict)
    for a in _rows(
        conn,
        """SELECT a.attrelid, a.attnum, a.attname, a.attnotnull,
                  format_type(a.atttypid, a.atttypmod) AS type,
                  pg_get_expr(d.adbin, d.adrelid) AS default
             FROM pg_attribute a
             LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attnum > 0 AND NOT a.attisdropped""",
    ):
        columns[a["attrelid"]][a["attnum"]] = a
    # Inheritance (and partitioning, which is built on it): a query on the parent reads the
    # children's rows under the parent's policies only, so no table in public may take part.
    for link in _rows(
        conn,
        """SELECT pc.relname AS parent, cc.relname AS child, pc.relnamespace = pn.oid AS p_public,
                  cc.relnamespace = pn.oid AS c_public
             FROM pg_inherits i
             JOIN pg_class pc ON pc.oid = i.inhparent
             JOIN pg_class cc ON cc.oid = i.inhrelid
             JOIN pg_namespace pn ON pn.nspname = 'public'
            WHERE pc.relnamespace = pn.oid OR cc.relnamespace = pn.oid""",
    ):
        problems.append(
            f"{link['child']}: inherits from {link['parent']}; tables in public must be "
            "neither partitioned nor in an inheritance hierarchy"
        )
    policies: dict[str, list[Any]] = defaultdict(list)
    for p in _rows(conn, "SELECT * FROM pg_policies WHERE schemaname = 'public'"):
        policies[p["tablename"]].append(p)
    indexes: dict[int, list[Any]] = defaultdict(list)
    for i in _rows(
        conn,
        """SELECT indrelid, indexrelid::regclass::text AS name, indisprimary, indnkeyatts,
                  string_to_array(indkey::text, ' ')::int[] AS keys
             FROM pg_index WHERE indisunique""",
    ):
        indexes[i["indrelid"]].append(i)
    constraints: dict[int, list[Any]] = defaultdict(list)
    for c in _rows(
        conn,
        """SELECT conrelid, conname, contype, conkey, confrelid, confkey, confdeltype,
                  confdelsetcols,
                  ARRAY(SELECT o.oprname FROM unnest(conexclop) WITH ORDINALITY u(op, i)
                          JOIN pg_operator o ON o.oid = u.op ORDER BY u.i) AS exclusion_ops
             FROM pg_constraint WHERE contype IN ('f', 'x')""",
    ):
        constraints[c["conrelid"]].append(c)

    def col_names(oid: int, keys: list[int] | None) -> list[str | None]:
        return [columns[oid][k]["attname"] if k in columns[oid] else None for k in (keys or [])]

    unlisted = {n for n in system_tables if not system_tables[n].strip()}
    problems += [f"{n}: SYSTEM_TABLES needs a reason" for n in sorted(unlisted)]
    is_owned = {
        oid: r["relkind"] in ("r", "p") and r["relname"] not in system_tables
        for oid, r in by_oid.items()
    }

    for oid, rel in by_oid.items():
        name = rel["relname"]
        names = {c["attname"]: c for c in columns[oid].values()}
        if rel["relkind"] not in ("r", "p"):
            problems.append(f"{name}: relation kind {rel['relkind']!r} is not allowed in public")
            continue
        if name in system_tables:
            if rel["relkind"] == "p":
                problems.append(f"{name}: partitioned tables are not allowed yet (#21)")
            if "owner_id" in names:
                problems.append(f"{name}: a system table must not have owner_id")
            for fk in constraints[oid]:
                if fk["contype"] == "f" and is_owned.get(fk["confrelid"]):
                    target = by_oid[fk["confrelid"]]["relname"]
                    problems.append(
                        f"{name}: foreign key {fk['conname']} into owned table {target}"
                    )
            continue
        problems += _check_owned(
            name, rel, names, policies[name], indexes[oid], constraints[oid], oid, by_oid,
            is_owned, col_names, global_unique,
        )  # fmt: skip
    return problems


def _check_owned(
    name: str, rel: Any, names: dict[str, Any], table_policies: list[Any], table_indexes: list[Any],
    table_constraints: list[Any], oid: int, by_oid: dict[int, Any], is_owned: dict[int, bool],
    col_names: Any, global_unique: Mapping[str, tuple[str, tuple[str, ...]]],
) -> list[str]:  # fmt: skip
    problems: list[str] = []

    def bad(message: str) -> None:
        problems.append(f"{name}: {message}")

    if rel["relkind"] == "p" or rel["relispartition"]:
        bad("owned tables must not be partitioned yet (#21 adds the partition rules)")
    row_id = names.get("id")
    if row_id is None or row_id["type"] != "uuid" or row_id["default"] != "gen_random_uuid()":
        bad("id must be uuid with the server default gen_random_uuid()")
    owner = names.get("owner_id")
    if owner is None:
        bad("no owner_id column (add it, or list the table in SYSTEM_TABLES with a reason)")
    elif owner["type"] != "uuid" or not owner["attnotnull"]:
        bad("owner_id must be uuid NOT NULL")
    if not rel["relrowsecurity"]:
        bad("row level security is not enabled")
    if not rel["relforcerowsecurity"]:
        bad("row level security is not forced")

    permissive = [p for p in table_policies if p["permissive"] == "PERMISSIVE"]
    if len(permissive) != 1:
        bad(f"expected exactly one permissive policy, found {len(permissive)}")
    for p in permissive:
        if p["policyname"] != "owner_isolation":
            bad(f"permissive policy {p['policyname']!r} must be named owner_isolation")
        if p["cmd"] != "ALL" or list(p["roles"]) != ["public"]:
            bad(f"policy {p['policyname']!r} must be FOR ALL TO PUBLIC")
        if p["qual"] != EXPECTED_EXPRESSION or p["with_check"] != EXPECTED_EXPRESSION:
            bad(f"policy {p['policyname']!r} has the wrong USING or WITH CHECK expression")

    denied = {
        p["cmd"]
        for p in table_policies
        if p["permissive"] == "RESTRICTIVE" and p["qual"] == "false" and p["with_check"] is None
        and list(p["roles"]) == ["public"]
    }  # fmt: skip
    for cmd in sorted(REQUIRED_DENY_POLICIES.get(name, set()) - denied):
        bad(f"needs a restrictive FOR {cmd} TO PUBLIC USING (false) policy")

    present = {
        (p["cmd"], p["qual"], p["with_check"])
        for p in table_policies
        if p["permissive"] == "RESTRICTIVE" and list(p["roles"]) == ["public"]
    }
    for cmd, qual, check in sorted(REQUIRED_POLICIES.get(name, set()) - present):
        bad(f"needs a restrictive FOR {cmd} TO PUBLIC USING ({qual}) WITH CHECK ({check}) policy")

    found_owner_id_unique = False
    for index in table_indexes:
        keys = col_names(oid, index["keys"][: index["indnkeyatts"]])
        if index["indisprimary"]:
            if keys != ["id"]:
                bad(f"primary key must be (id), is {keys}")
        elif keys == ["owner_id", "id"]:
            found_owner_id_unique = True
        elif "owner_id" not in keys and global_unique.get(index["name"]) != (name, tuple(keys)):
            bad(f"unique index {index['name']} {keys} does not include owner_id")
    if not any(i["indisprimary"] for i in table_indexes):
        bad("no primary key on id")
    if not found_owner_id_unique:
        bad("missing UNIQUE (owner_id, id)")

    for c in table_constraints:
        if c["contype"] == "x":
            pairs = list(zip(col_names(oid, c["conkey"]), c["exclusion_ops"], strict=True))
            if ("owner_id", "=") not in pairs:
                bad(f"exclusion constraint {c['conname']} must include owner_id WITH =")
            continue
        target_oid = c["confrelid"]
        child = col_names(oid, c["conkey"])
        parent = col_names(target_oid, c["confkey"])
        if is_owned.get(target_oid) and ("owner_id", "owner_id") not in list(
            zip(child, parent, strict=True)
        ):
            bad(f"foreign key {c['conname']} must map owner_id to the parent's owner_id")
        action = c["confdeltype"]
        if action in SET_ACTIONS:
            if not c["confdelsetcols"] or "owner_id" in col_names(oid, c["confdelsetcols"]):
                bad(f"foreign key {c['conname']}: {SET_ACTIONS[action]} needs a column list "
                    "without owner_id")  # fmt: skip
        elif action not in SAFE_DELETE_ACTIONS:
            bad(f"foreign key {c['conname']} has an unsupported ON DELETE action")
    return problems
