"""Guard 8 of ADR 0003 section 1, and proof that it can fail."""

from pathlib import Path

import pytest

from auth_tables_scan import scan_source, scan_source_with_exemptions

SRC = Path(__file__).resolve().parent.parent / "src"
EXPECTED_EXEMPTIONS = 0  # strings marked "# guard8: prose" (raise it only with a reason in review)


def test_nothing_outside_ownvitals_auth_touches_the_account_tables() -> None:
    violations = []
    for path in sorted((SRC / "ownvitals").rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith("ownvitals/auth/"):
            continue
        violations += [(relative, line, why) for line, why in scan_source(path.read_text("utf-8"))]
    assert violations == []


def test_the_scan_covers_the_auth_package_itself_in_one_place_only() -> None:
    inside = [p for p in (SRC / "ownvitals" / "auth").rglob("*.py") if p.name == "tables.py"]
    assert len(inside) == 1  # the one module the guard protects


ALLOWED = [
    "from ownvitals import auth\nauth.create_invite",
    "from ownvitals.auth import purge_expired, create_invite, SessionInfo",
    "import ownvitals.auth as auth\nauth.purge_expired",
    "from ownvitals import auth\nauth.require_user\ngetattr(auth, 'purge_expired')",
    "from sqlalchemy import table\ntable('other')",
    "import importlib\nimportlib.import_module('ownvitals.context')",
    "from . import auth",
    "from .auth import purge_expired",
    "x = 'select * from somewhere_else'",
    "x = 'the users of sessions are listed'",  # words, no SQL keyword before them
    "def f(users, sessions): return users",
    "from ownvitals.db.tables import SYSTEM_TABLES",
    "s = 'SELECT 1 FROM alembic_version'",
    "x = 'purge of expired sessions finished'",  # log messages and docstrings: no SQL verb
    "x = 'all users are listed in the account page'",
    "import ownvitals\nownvitals.auth.create_invite",
    "import ownvitals as c\nc.auth.purge_expired\nc.cli.run_command",
    "from .. import ownvitals\nownvitals.auth.require_user",
    "from sqlalchemy import table\ntable('other', name='users_view_cache_x')",
    "import importlib\nimportlib.import_module('ownvitals.auth').purge_expired",
    "import pkgutil\npkgutil.resolve_name('ownvitals.context:UserContext')",
    "x = vars(object())",
    "x = 'select the data copied from sessions_archive'",  # a different name
    "x = 'Showing ' + str(n) + ' users'",
    "x = 1 + 2",
    "x = 'the table sessions is described in the docs'",
    "__import__('ownvitals.context').context",
    "__import__('ownvitals.auth').auth.create_invite",
    # the public entry points (#10)
    "from ownvitals.auth.setup import attach_state, detach_state, install",
    "from ownvitals.auth.setup import install as install_auth",
    "from ownvitals.auth.deps import Probe, require_user, session_probe",
    # the step-up dependencies (#11), for #12 and #13
    "from ownvitals.auth.deps import require_admin_stepup, require_sensitive, require_stepup",
    "from ownvitals.auth.owners import ACCOUNTS",
    "from ownvitals.auth.purge import purge_expired",
    "from ownvitals.auth.operator import AccountError, create_invite, mfa_reset",
]
REFUSED = {
    "import module": "import ownvitals.auth.tables",
    "from import": "from ownvitals.auth.tables import users",
    "from submodule import": "from ownvitals.auth.tables import sessions as s",
    "package import": "from ownvitals.auth import tables",
    "submodule import": "from ownvitals.auth.sessions import SessionInfo",
    "entry point module, other name": "from ownvitals.auth.purge import sessions",
    "entry point module, unlisted name": "from ownvitals.auth.deps import resolve_request",
    "entry point module, unlisted dependency": "from ownvitals.auth.deps import require_session",
    "entry point module, star": "from ownvitals.auth.setup import *",
    "entry point module, relative": "from .auth.purge import purge_expired",
    "entry point module, imported whole": "import ownvitals.auth.setup",
    "entry point module from the package": "from ownvitals.auth import setup",
    "submodule table": "from ownvitals.auth.sessions import sessions, users",
    "submodule alias": "import ownvitals.auth.purge as p\np.sessions",
    "import submodule": "import ownvitals.auth.keys",
    "table name from package": "from ownvitals.auth import sessions",
    "metadata from package": "from ownvitals.auth import metadata",
    "relative submodule": "from .auth.sessions import SessionInfo",
    "relative package submodule": "from .auth import tables",
    "getattr submodule": "from ownvitals import auth\ngetattr(auth, 'tables')",
    "getattr table": "import ownvitals.auth as a\ngetattr(a, 'users')",
    "table attribute on alias": "from ownvitals import auth\nauth.sessions",
    "metadata attribute": "import ownvitals.auth\nownvitals.auth.metadata",
    "import_module submodule": (
        "from importlib import import_module\nimport_module('ownvitals.auth.sessions')"
    ),
    "relative import_module": (
        "import importlib\nimportlib.import_module('.sessions', 'ownvitals.auth')"
    ),
    "__import__": "__import__('ownvitals.auth.tables')",
    "sqlalchemy table": "from sqlalchemy import table\ntable('sessions')",
    "sqlalchemy Table": "import sqlalchemy as sa\nsa.Table('users', sa.MetaData())",
    "star import": "from ownvitals.auth import *",
    "ownvitals alias": "import ownvitals as c\nc.auth.tables",
    "ownvitals alias table": "import ownvitals as c\nc.auth.sessions",
    "ownvitals package": "import ownvitals\nownvitals.auth.sessions",
    "ownvitals relative root": "from .. import ownvitals\nownvitals.auth.users",
    "import_module attribute": (
        "import importlib\nimportlib.import_module('ownvitals.auth').tables"
    ),
    "import_module assigned": (
        "import importlib\na = importlib.import_module('ownvitals.auth')\na.users"
    ),
    "__import__ attribute": "__import__('ownvitals.auth').metadata",
    "getattr on import_module": (
        "import importlib\ngetattr(importlib.import_module('ownvitals.auth'), 'tables')"
    ),
    "getattr on ownvitals alias": "import ownvitals as c\ngetattr(c.auth, 'users')",
    "table keyword": "from sqlalchemy import table\ntable(name='users')",
    "Table keyword": "import sqlalchemy as sa\nsa.Table(name='sessions', metadata=m)",
    "vars": "from ownvitals import auth\nvars(auth)",
    "__dict__": "from ownvitals import auth\nauth.__dict__",
    "__dict__ via ownvitals": "import ownvitals as c\nc.auth.__dict__",
    "resolve_name": "import pkgutil\npkgutil.resolve_name('ownvitals.auth:tables')",
    "comma join": "q = 'SELECT 1 FROM a, users'",
    "quoted schema": 'q = \'select 1 from "public"."users"\'',
    "using": "q = 'DELETE FROM other USING sessions WHERE other.id = sessions.id'",
    "copy": "q = 'COPY users TO STDOUT'",
    "parentheses": "q = 'select * from (select id from invites) i'",
    "comments": "q = '-- note\\nSELECT 1 /* c */ FROM /* c */ auth_tokens'",
    "alter": "q = 'ALTER TABLE mfa_totp ADD COLUMN x int'",
    "merge": "q = 'MERGE INTO known_devices USING t ON true'",
    "with cte": "q = 'WITH x AS (SELECT 1) SELECT * FROM oauth_flows'",
    "f-string": "q = f'SELECT * FROM users WHERE id = {i}'",
    "text keyword": "q = text(statement='select 1 from identities')",
    "schema": "q = 'select 1 from public.pending_signups'",
    "attribute": "import ownvitals.auth\nownvitals.auth.tables.users",
    "alias attribute": "from ownvitals import auth\nauth.tables.sessions",
    "as alias attribute": "import ownvitals.auth as a\na.tables.users",
    "importlib": "import importlib\nimportlib.import_module('ownvitals.auth.tables')",
    "select": "q = text('SELECT * FROM users WHERE id = :i')",
    "join": "q = 'SELECT 1 FROM x JOIN sessions s ON s.user_id = x.id'",
    "insert": "q = 'INSERT INTO invites (code_hash) VALUES (1)'",
    "update": "q = 'UPDATE users SET role = 1'",
    "delete": "q = 'DELETE FROM rate_limits'",
    "truncate": "q = 'TRUNCATE auth_events'",
    "qualified": "q = 'SELECT * FROM public.mfa_totp'",
    "quoted": "q = 'SELECT * FROM \"known_devices\"'",
    "lower case": "q = 'select * from oauth_flows'",
    "multi-line": "q = '''\\nSELECT *\\n  FROM\\n  pending_signups'''",
    "lock": "q = 'LOCK TABLE auth_tokens'",
    "recovery": "q = 'delete from mfa_recovery_codes'",
    "identities": "q = 'select 1 from identities'",
    "__import__ then auth.tables": "__import__('ownvitals.auth').auth.tables",
    "__import__ then auth.sessions": "__import__('ownvitals.auth').auth.sessions",
    "__import__ assigned": "c = __import__('ownvitals.auth')\nc.auth.users",
    "__import__ ownvitals": "__import__('ownvitals').auth.tables",
    "import_module ownvitals": (
        "import importlib\nimportlib.import_module('ownvitals').auth.metadata"
    ),
    "truncate without TABLE": "q = 'TRUNCATE users, sessions'",
    "only modifier": "q = 'DELETE FROM ONLY users'",
    "drop if exists": "q = 'DROP TABLE IF EXISTS invites'",
    "from list with alias": "q = 'select 1 from a x, b as y, auth_events'",
    "keyword then comment": "q = 'select 1 from /* c */ users'",
    # verb and table in different pieces (review of #55)
    "f-string column list": 'q = text(f"SELECT {cols} FROM users WHERE id = :id")',
    "f-string from list": 'q = f"SELECT {c} FROM a, users"',
    "f-string delete": 'q = f"DELETE {x} FROM users"',
    "plus chain from": 'q = "SELECT * " + "FROM users"',
    "plus chain into": 'q = "INSERT " + "INTO invites VALUES (1)"',
    "plus chain join": 'q = "SELECT * FROM a " + "JOIN sessions ON true"',
    "plus chain with a name": 'q = "SELECT " + cols + " FROM users"',
    "plus chain in a call": 'q = text("SELECT * " + "FROM " + "users")',
    "bare fragment": 'q = " FROM users WHERE id = :id"',
    "bare fragment start": 'q = "FROM sessions"',
    "bare join fragment": 'q = " JOIN identities i ON i.user_id = u.id"',
    "bare update fragment": 'q = "UPDATE users SET role = 1"',
    "bare fragment with alias": 'q = " FROM users u WHERE u.id = 1"',
    "bare fragment semicolon": 'q = "x FROM users;"',
    "fragment in an f-string": 'q = f" FROM users WHERE id = {i}"',
    # narrowed in the first version (review of #55)
    "grant on": "q = 'GRANT SELECT ON users TO x'",
    "revoke on": "q = 'REVOKE ALL ON sessions FROM x'",
    "grant on table": "q = 'GRANT ALL ON TABLE invites TO x'",
    "any schema": "q = 'select * from ownvitals.users'",
    "any quoted schema": 'q = \'select * from "app"."users"\'',
    "quoted without space": "q = 'SELECT count(*) FROM\"users\"'",
    "join quoted without space": "q = 'select 1 from x join\"sessions\" on true'",
    # shapes main refused that the first rewrite lost (second review of #55)
    "truncate list": "q = 'TRUNCATE a, users'",
    "truncate table list": "q = 'TRUNCATE TABLE a, users'",
    "lock table list": "q = 'LOCK TABLE a, users IN ACCESS EXCLUSIVE MODE'",
    "using list": "q = 'DELETE FROM a USING b, users WHERE true'",
    "grant on list": "q = 'GRANT SELECT ON a, users TO x'",
    "revoke on table list": "q = 'REVOKE ALL ON TABLE a, sessions FROM x'",
    "only in a from list": "q = 'select 1 from a, ONLY users'",
    "column alias list": "q = 'select 1 from a x(c1), users'",
    "join then comma": "q = 'select 1 from a join b on a.id = b.id, users'",
    "parenthesised join": "q = 'select 1 from (users join x on true)'",
    "lateral": "q = 'select 1 from a, lateral (select 1) l, users'",
    "subquery before the table": "q = 'select 1 from a, (select 1) b, users'",
    "join in parentheses": "q = 'select 1 from a join (users) on true'",
    "builder left join": "sql = 'SELECT c.id FROM classes c'\nsql += ' LEFT JOIN users u'",
    "builder on clause": "sql += ' ON u.id = c.owner_id'\nq = ' LEFT JOIN users u'",
    "builder inner join": "sql += ' INNER JOIN sessions s'",
    "builder base plus list": "q = base + ' FROM a, users'",
    "builder f-string list": "q = f'{base} FROM a, users'",
    "builder columns": "q = ' u.id, u.email FROM users'",
    # prose is refused unless marked (the exemption is the way out, see below)
    "prose copied from": "x = 'copied from sessions'",
    "prose in a longer sentence": "x = 'files copied from sessions of the old host'",
    "prose without adjacency": "x = 'Rows are copied from the sessions of the user into a report'",
    "prose update": "x = 'update the sessions after a select of users'",
    "prose delete": "x = 'delete the invites of the group'",
    "prose in an f-string": "x = f'{n} rows copied from sessions of {u}'",
    "prose in a plus chain": "x = 'rows ' + 'copied from sessions'",
    "marker on another line": "# guard8: prose\nx = 'copied from sessions'",
    "marker inside the string": "x = 'copied from sessions # guard8: prose'",
    # the marker inside f-string text, or as a comment inside a replacement field, is not after it
    "marker in f-string text": "x = f'copied from sessions {n} # guard8: prose'",
    "marker in f-string text, multi-line": "x = f'''copied from sessions {n}\n# guard8: prose'''",
    "marker in a replacement field": "x = f'{ # guard8: prose\n n} copied from sessions'",
    "marker in a nested replacement field": (
        "x = f'{ f\"{ # guard8: prose\n n}\" } copied from sessions'"
    ),
    "marker after a multi-line f-string": (
        "x = f'''{\n n  # note\n} copied from sessions'''  # guard8: prose"
    ),
    "marker on a docstring-like second string": '"""doc."""\nx = """copied from sessions"""',
}


@pytest.mark.parametrize("source", ALLOWED)
def test_allowed(source: str) -> None:
    assert scan_source(source) == []


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_refused(source: str) -> None:
    assert scan_source(source) != []


def test_the_auth_package_does_not_re_export_a_table_object() -> None:
    from sqlalchemy import MetaData, Table

    import ownvitals.auth as package
    from auth_tables_scan import TABLE_NAMES

    for name in dir(package):
        assert not isinstance(getattr(package, name), Table | MetaData), name
    assert not {"metadata", *TABLE_NAMES} & set(getattr(package, "__all__", ()))


def test_each_public_entry_point_exists_and_is_no_table() -> None:
    """The names of ``PUBLIC_ENTRY_POINTS`` (#10) are real and are none of the table objects."""
    import importlib

    from sqlalchemy import MetaData, Table

    from auth_tables_scan import FORBIDDEN_NAMES, PUBLIC_ENTRY_POINTS

    assert PUBLIC_ENTRY_POINTS
    for module_name, names in PUBLIC_ENTRY_POINTS.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert name not in FORBIDDEN_NAMES, (module_name, name)
            value = getattr(module, name)
            assert not isinstance(value, Table | MetaData), (module_name, name)


def test_the_entry_points_are_the_only_submodule_names_outside_the_package() -> None:
    from auth_tables_scan import PUBLIC_ENTRY_POINTS

    for module_name, names in PUBLIC_ENTRY_POINTS.items():
        assert scan_source(f"from {module_name} import {', '.join(sorted(names))}") == []
        assert scan_source(f"from {module_name} import users") != []
        assert scan_source(f"from {module_name} import metadata") != []
        assert scan_source(f"from {module_name} import not_listed") != []
        assert scan_source(f"import {module_name}") != []


# --- exemptions: docstrings and the marked line ------------------------------------------------

EXEMPT_SOURCES = [
    '"""Rows are copied from sessions."""',
    'def f():\n    """Copied from sessions of the user."""\n    return 1',
    'class C:\n    """Delete the rows from users."""',
    'async def f():\n    """select 1 from users"""',
    "x = 'copied from sessions'  # guard8: prose",
    "x = f'{n} copied from sessions'  # guard8: prose",
    "x = 'rows ' + 'copied from sessions'  # guard8: prose",
    "x = 'copied from sessions'  # guard8:prose",
    "x = (\n    'copied from sessions'  # guard8: prose\n)",
]


@pytest.mark.parametrize("source", EXEMPT_SOURCES)
def test_docstrings_and_marked_lines_are_exempt(source: str) -> None:
    assert scan_source_with_exemptions(source)[0] == []


def test_the_scanner_counts_the_exemptions() -> None:
    marked = "a = 'copied from sessions'  # guard8: prose\nb = 'delete from users'  # guard8: prose"
    assert scan_source_with_exemptions(marked) == ([], 2)
    docstring = '"""copied from sessions"""'
    assert scan_source_with_exemptions(docstring) == ([], 0)  # docstrings are not counted
    assert scan_source_with_exemptions("x = 'nothing'  # guard8: prose")[1] == 0


def test_a_marker_inside_a_multi_line_string_exempts_nothing() -> None:
    inside = "x = '''\ncopied from sessions\n# guard8: prose\n'''"
    assert scan_source_with_exemptions(inside) == ([(1, "raw SQL naming table sessions")], 0)
    first_line = "x = '''copied from sessions # guard8: prose\nmore'''"
    assert scan_source_with_exemptions(first_line)[1] == 0
    assert scan_source_with_exemptions(first_line)[0] != []
    # a real comment after the string, on the line where it starts, still exempts
    real = "x = ('''copied from sessions''')  # guard8: prose"
    assert scan_source_with_exemptions(real) == ([], 1)


def test_a_marker_inside_an_fstring_exempts_nothing_and_counts_nothing() -> None:
    """Issue #73: the decision is that only a comment *after* the string exempts it."""
    for source in (
        "x = f'copied from sessions {n} # guard8: prose'",
        "x = f'{ # guard8: prose\n n} copied from sessions'",
    ):
        findings, exempted = scan_source_with_exemptions(source)
        assert findings != [] and exempted == 0, source  # noqa: PT018
    # The same f-string with the marker after it still exempts (and counts once).
    assert scan_source_with_exemptions("x = f'{n} copied from sessions'  # guard8: prose") == (
        [],
        1,
    )
    # A replacement-field comment elsewhere in the source does not disturb a real marker.
    both = "a = f'{ # note\n n}'\nb = 'copied from sessions'  # guard8: prose"
    assert scan_source_with_exemptions(both) == ([], 1)


def test_exemptions_are_counted_per_string() -> None:
    two = "f('copied from sessions', 'delete from users')  # guard8: prose"
    assert scan_source_with_exemptions(two) == ([], 2)
    chain = "x = 'rows ' + 'copied from sessions'  # guard8: prose"
    assert scan_source_with_exemptions(chain) == ([], 1)


def test_a_docstring_is_only_the_first_statement() -> None:
    later = "def f():\n    x = 1\n    'copied from sessions'\n"
    assert scan_source(later) != []  # not a docstring: it is not the first statement


def test_a_marker_does_not_exempt_real_sql_elsewhere() -> None:
    source = "a = 'prose'  # guard8: prose\nq = 'select 1 from users'"
    assert scan_source(source) != []


def test_the_whole_tree_needs_no_exemption() -> None:
    """The number of ``# guard8: prose`` exemptions in the source tree is pinned (none today)."""
    total = 0
    for path in sorted((SRC / "ownvitals").rglob("*.py")):
        if path.relative_to(SRC).as_posix().startswith("ownvitals/auth/"):
            continue
        total += scan_source_with_exemptions(path.read_text("utf-8"))[1]
    assert total == EXPECTED_EXEMPTIONS
