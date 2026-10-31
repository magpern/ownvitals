"""Guard 1 of ADR 0002 section 7, on the real schema, plus proof that it can fail. Guard 2 is in
``test_role_guard.py``."""

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from dbtools import PARENT, drop_tables, migrated, owner_connection, run_rls_helper
from ownvitals.db import SYSTEM_TABLES
from rls_guard import check_tables

SCRATCH = ("t_bad", "t_sys")
GOOD = (
    "CREATE TABLE t_bad (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
    "owner_id uuid NOT NULL, name text, UNIQUE (owner_id, id){extra})"
)


def problems_of(owner_url: str, system: dict[str, str] | None = None) -> list[str]:
    with owner_connection(owner_url) as connection:
        return check_tables(connection, {**SYSTEM_TABLES, **(system or {})})


@pytest.fixture
def schema(owner_database_url: str) -> Iterator[None]:
    """The migrated schema (all revisions), as ``migrate`` leaves it."""
    with migrated(owner_database_url):
        yield


@pytest.mark.usefixtures("schema")
def test_the_migrated_schema_meets_the_contract(database_url: str) -> None:
    assert all(reason.strip() for reason in SYSTEM_TABLES.values())
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert check_tables(connection, SYSTEM_TABLES) == []
            tables = connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
            assert set(tables) == set(SYSTEM_TABLES)  # nothing owned exists yet
    finally:
        engine.dispose()


def test_the_test_tables_made_with_the_helper_meet_the_contract(
    owned_tables: None, owner_database_url: str
) -> None:
    assert problems_of(owner_database_url) == []


@pytest.fixture
def scratch(owned_tables: None, owner_database_url: str) -> Iterator[Connection]:
    """A transaction as the owner for building one deliberately wrong table."""
    drop_tables(owner_database_url, *SCRATCH)
    with owner_connection(owner_database_url) as connection:
        yield connection
    drop_tables(owner_database_url, *SCRATCH)


def violations(connection: Connection, system: dict[str, str] | None = None) -> list[str]:
    return check_tables(connection, {**SYSTEM_TABLES, **(system or {})})


def exec_all(connection: Connection, *statements: str) -> None:
    for statement in statements:
        connection.execute(text(statement))


def good(extra: str = "") -> str:
    return GOOD.format(extra=extra)


def test_an_exclusion_constraint_with_owner_id_passes(scratch: Connection) -> None:
    exec_all(scratch, good(", EXCLUDE USING btree (owner_id WITH =, name WITH =)"))
    run_rls_helper(scratch, "t_bad")
    assert violations(scratch) == []


def test_a_good_table_with_a_restrictive_extra_policy_and_set_null_columns_passes(
    scratch: Connection,
) -> None:
    exec_all(
        scratch,
        good(
            f", parent_id uuid, FOREIGN KEY (owner_id, parent_id) REFERENCES {PARENT} "
            "(owner_id, id) ON DELETE SET NULL (parent_id)"
        ),
    )
    run_rls_helper(scratch, "t_bad")
    exec_all(scratch, "CREATE POLICY narrow ON t_bad AS RESTRICTIVE USING (name <> 'x')")
    assert violations(scratch) == []


CASES = {
    "no rls": (
        [good()],
        "row level security is not enabled",
    ),
    "not forced": (
        [good(), "ALTER TABLE t_bad ENABLE ROW LEVEL SECURITY"],
        "row level security is not forced",
    ),
    "second permissive policy": (
        [good(), "RLS", "CREATE POLICY open ON t_bad USING (true)"],
        "expected exactly one permissive policy, found 2",
    ),
    "wrong expression": (
        [
            good(),
            "ALTER TABLE t_bad ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE t_bad FORCE ROW LEVEL SECURITY",
            "CREATE POLICY owner_isolation ON t_bad USING (true) WITH CHECK (true)",
        ],
        "wrong USING or WITH CHECK",
    ),
    "policy for one role": (
        [
            good(),
            "ALTER TABLE t_bad ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE t_bad FORCE ROW LEVEL SECURITY",
            "CREATE POLICY owner_isolation ON t_bad TO ownvitals_app USING (true)",
        ],
        "FOR ALL TO PUBLIC",
    ),
    "nullable owner_id": (
        [
            "CREATE TABLE t_bad (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), owner_id uuid, "
            "UNIQUE (owner_id, id))",
            "RLS",
        ],
        "owner_id must be uuid NOT NULL",
    ),
    "id is text": (
        [
            "CREATE TABLE t_bad (id text PRIMARY KEY, owner_id uuid NOT NULL, "
            "UNIQUE (owner_id, id))",
            "RLS",
        ],
        "id must be uuid with the server default",
    ),
    "id without a server default": (
        [
            "CREATE TABLE t_bad (id uuid PRIMARY KEY, owner_id uuid NOT NULL, "
            "UNIQUE (owner_id, id))",
            "RLS",
        ],
        "id must be uuid with the server default",
    ),
    "id with another default": (
        [
            "CREATE TABLE t_bad (id uuid PRIMARY KEY DEFAULT (md5(random()::text))::uuid, "
            "owner_id uuid NOT NULL, UNIQUE (owner_id, id))",
            "RLS",
        ],
        "id must be uuid with the server default",
    ),
    "exclusion constraint without owner_id": (
        [good(", EXCLUDE USING btree (name WITH =)"), "RLS"],
        "must include owner_id WITH =",
    ),
    "no owner_id": (
        ["CREATE TABLE t_bad (id uuid PRIMARY KEY DEFAULT gen_random_uuid())"],
        "no owner_id column",
    ),
    "no unique owner_id, id": (
        [
            "CREATE TABLE t_bad (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
            "owner_id uuid NOT NULL)",
            "RLS",
        ],
        "missing UNIQUE (owner_id, id)",
    ),
    "global unique": (
        [good(", UNIQUE (name)"), "RLS"],
        "does not include owner_id",
    ),
    "global unique index": (
        [good(), "CREATE UNIQUE INDEX t_bad_name ON t_bad (lower(name))", "RLS"],
        "does not include owner_id",
    ),
    "unique without owner_id": (
        [good(), "CREATE UNIQUE INDEX t_bad_id ON t_bad (id, name)", "RLS"],
        "does not include owner_id",
    ),
    "partitioned": (
        [
            "CREATE TABLE t_bad (id uuid NOT NULL, owner_id uuid NOT NULL) "
            "PARTITION BY LIST (owner_id)",
            "RLS",
        ],
        "must not be partitioned",
    ),
    "partitioned with the amendment 2 key": (
        [
            "CREATE TABLE t_bad (id uuid NOT NULL DEFAULT gen_random_uuid(), "
            "owner_id uuid NOT NULL, ts timestamptz NOT NULL, PRIMARY KEY (owner_id, id, ts)) "
            "PARTITION BY RANGE (ts)",
            "RLS",
        ],
        "must not be partitioned yet (#21",
    ),
    "foreign key without the owner": (
        [good(f", parent_id uuid REFERENCES {PARENT} (id)"), "RLS"],
        "must map owner_id to the parent's owner_id",
    ),
    "swapped foreign key": (
        [
            f"CREATE UNIQUE INDEX t_parent_swapped ON {PARENT} (id, owner_id)",
            good(
                f", parent_id uuid, FOREIGN KEY (owner_id, parent_id) REFERENCES {PARENT} "
                "(id, owner_id)"
            ),
            "RLS",
        ],
        "must map owner_id to the parent's owner_id",
    ),
    "bare set null": (
        [
            good(
                f", parent_id uuid, FOREIGN KEY (owner_id, parent_id) REFERENCES {PARENT} "
                "(owner_id, id) ON DELETE SET NULL"
            ),
            "RLS",
        ],
        "needs a column list without owner_id",
    ),
    "set null including owner_id": (
        [
            good(
                f", parent_id uuid, FOREIGN KEY (owner_id, parent_id) REFERENCES {PARENT} "
                "(owner_id, id) ON DELETE SET NULL (owner_id, parent_id)"
            ),
            "RLS",
        ],
        "needs a column list without owner_id",
    ),
}


@pytest.mark.parametrize(("statements", "expected"), CASES.values(), ids=CASES.keys())
def test_the_guard_fails_for_a_table_that_breaks_the_contract(
    scratch: Connection, statements: list[str], expected: str
) -> None:
    for statement in statements:
        if statement == "RLS":
            run_rls_helper(scratch, "t_bad")
        else:
            scratch.execute(text(statement))
    found = violations(scratch)
    assert any(expected in problem for problem in found), found
    assert all(problem.startswith("t_bad:") for problem in found), found


def test_a_view_in_public_fails(scratch: Connection) -> None:
    exec_all(scratch, "CREATE VIEW t_view AS SELECT * FROM " + PARENT)  # noqa: S608
    try:
        assert violations(scratch) == ["t_view: relation kind 'v' is not allowed in public"]
    finally:
        exec_all(scratch, "DROP VIEW t_view")


def test_a_system_table_must_not_have_owner_id(scratch: Connection) -> None:
    exec_all(scratch, "CREATE TABLE t_sys (id uuid PRIMARY KEY, owner_id uuid)")
    found = violations(scratch, {"t_sys": "test"})
    assert found == ["t_sys: a system table must not have owner_id"]


def test_a_system_table_must_not_reference_an_owned_table(scratch: Connection) -> None:
    exec_all(
        scratch, f"CREATE TABLE t_sys (id uuid PRIMARY KEY, ref uuid REFERENCES {PARENT} (id))"
    )
    found = violations(scratch, {"t_sys": "test"})
    assert [p for p in found if "into owned table" in p], found


def test_a_partitioned_system_table_fails_too(scratch: Connection) -> None:
    exec_all(scratch, "CREATE TABLE t_sys (ts timestamptz NOT NULL) PARTITION BY RANGE (ts)")
    found = violations(scratch, {"t_sys": "test"})
    assert found == ["t_sys: partitioned tables are not allowed yet (#21)"]


def test_only_the_registered_global_unique_index_may_leave_out_owner_id(
    scratch: Connection,
) -> None:
    """ADR 0002 section 2: one owner-less unique index is sanctioned, by table, name and columns
    (#22's ``connections (app_id, withings_userid)``); any other one fails."""
    exec_all(scratch, good(", app_id uuid, ext text"), "CREATE UNIQUE INDEX t_bad_ext ON t_bad "
             "(app_id, ext)")  # fmt: skip
    run_rls_helper(scratch, "t_bad")
    sanctioned = {"t_bad_ext": ("t_bad", ("app_id", "ext"))}
    system = dict(SYSTEM_TABLES)
    assert check_tables(scratch, system, sanctioned) == []
    for registered in (
        {},  # not registered
        {"t_bad_ext": ("t_bad", ("app_id",))},  # other columns
        {"t_bad_ext": ("t_parent", ("app_id", "ext"))},  # another table
        {"t_bad_other": ("t_bad", ("app_id", "ext"))},  # another name
    ):
        found = check_tables(scratch, system, registered)
        assert found == [
            "t_bad: unique index t_bad_ext ['app_id', 'ext'] does not include owner_id"
        ]


def test_a_system_table_needs_a_reason(scratch: Connection) -> None:
    exec_all(scratch, "CREATE TABLE t_sys (id uuid PRIMARY KEY)")
    assert violations(scratch, {"t_sys": " "}) == ["t_sys: SYSTEM_TABLES needs a reason"]


def test_an_unlisted_table_without_the_contract_fails(scratch: Connection) -> None:
    exec_all(scratch, "CREATE TABLE t_sys (id uuid PRIMARY KEY)")
    assert any("no owner_id column" in p for p in violations(scratch))


def test_inheritance_leaks_rows_across_users_and_the_guard_refuses_it(
    owner_database_url: str, app_database_url: str, owned_tables: None
) -> None:
    """The review's case: an owned child inherits a system table; A reads B through the parent."""
    user_a, user_b = "00000000-0000-0000-0000-00000000000a", "00000000-0000-0000-0000-00000000000b"
    drop_tables(owner_database_url, "t_bad", "t_sys")
    with owner_connection(owner_database_url) as connection:
        exec_all(connection, "CREATE TABLE t_sys (name text)", good() + " INHERITS (t_sys)")
        run_rls_helper(connection, "t_bad")
    app = create_engine(app_database_url)
    try:
        with app.begin() as connection:
            connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": user_b})
            connection.execute(
                text("INSERT INTO t_bad (owner_id, name) VALUES (:o, 'B-secret')"), {"o": user_b}
            )
        with app.begin() as connection:
            connection.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": user_a})
            own = connection.execute(text("SELECT name FROM t_bad")).scalars().all()
            via_parent = connection.execute(text("SELECT name FROM t_sys")).scalars().all()
        assert own == []
        assert via_parent == ["B-secret"], "the hole: Postgres applies only the parent's policy"
        found = problems_of(owner_database_url, {"t_sys": "test"})
        assert any("inheritance hierarchy" in p for p in found), found
    finally:
        app.dispose()
        drop_tables(owner_database_url, "t_bad", "t_sys")
