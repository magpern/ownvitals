"""ADR 0006 section 11's guard: findings and ``estimated`` are read for presentation only through
``ownvitals.views.findings_read``, so tier visibility, the experimental switch, the overlay and
RLS cannot be bypassed by a second query path.

The guard is **structural**, not a pattern for SQL (the Architect, after the #228 review): outside
the modules of ``ALLOWED`` (exact names, each with its reason), the tables' identifiers do not
appear at all.

``scan_source`` parses one module of ``src/ownvitals`` and refuses:

- **any string** (every ``str`` constant: whole strings, the parts of a ``+`` chain, ``%`` and
  ``.format`` templates and their arguments, the constant pieces of an f-string, ``Literal``
  arguments, dict keys) that holds ``findings``, ``estimated`` or ``ecg_recordings`` as a word,
  in any letter case. A word is bounded by anything but a letter, a digit or ``_``, so
  ``estimated->>'hr_bpm'``, ``"findings"``, ``public.findings`` and ``r.estimated`` all count, and
  ``experimental_findings`` does not. This covers SQL on the tables (whole-row reads of
  ``ecg_recordings`` too: ``SELECT *``, ``r.*`` and ``to_jsonb(r)`` all name the table), Core by
  name (``metadata.tables["findings"]``, ``table("findings")``, ``.c["estimated"]``,
  ``getattr(m, "findings")``) and anything else spelled as text. Docstrings are prose and not
  scanned. A string reviewed as harmless is listed exactly, by module, in ``REVIEWED_STRINGS``
  with the number of times it occurs there: one more occurrence (``table("findings")`` in a
  module that may say ``"findings"`` as a route key) is refused, and each pin must match ``src``;
- **the Table objects by name**: an import of ``findings`` or ``ecg_recordings`` from any
  module (a re-export too: ``from ownvitals.connections.purge import findings``), any ``*``
  import, and the attribute ``.findings`` or ``.ecg_recordings`` on a name an import bound
  (``tables.findings``, ``t.ecg_recordings``, ``ownvitals.derived.tables.findings``), so
  ``select(ecg_recordings)`` needs one of them;
- **the column by name**: ``<x>.c.estimated`` and ``<x>.columns.estimated``.

``scan_sql`` covers the database side (N4 of the review): a migration string that creates a view,
materialized view, rule, function or procedure naming one of the three, and any SQL file under
``docker/postgres`` that names one (a ``SECURITY DEFINER`` function there would read past RLS).
None exists; one would extend this guard in its own PR, with its reason.

What it does not stop (the review gate does): a name built at run time (``"find" + "ings"``,
``getattr`` with a computed name, ``importlib``, ``exec``), reflection, and code outside ``src``,
migrations and ``docker/postgres``. A new allowed module (the rules engine's findings writer, the
``estimated`` recompute's sweep) joins ``ALLOWED`` in its own PR, with its reason;
``test_the_allow_list_is_exact`` pins the list and requires every entry to need it.
"""

import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
SRC = BACKEND / "src"
MIGRATIONS = BACKEND / "migrations" / "versions"
DOCKER_SQL = BACKEND.parent / "docker" / "postgres"
READ_FUNCTION = "ownvitals.views.findings_read"
# ADR 0006 section 11, by exact module name: the read function, the definitions, and the writes
# and operator reads the section allows. Each names a table today (the exactness test).
ALLOWED: dict[str, str] = {
    READ_FUNCTION: "the one read function (ADR 0006 section 11)",
    "ownvitals.derived.tables": "defines the findings table",
    "ownvitals.withings.tables": "defines ecg_recordings and its estimated column",
    "ownvitals.ingest.rows": "ingest writes ecg_recordings; the recompute writes estimated (§15)",
    "ownvitals.ecg_validate": "ownvitals ecg validate (#47) reads the stored estimated (V5)",
    "ownvitals.connections.purge": "the purge path: delete imported data removes both tables",
    # #187: the rules engine's findings writer (ADR 0010 §4 episodes; ADR 0006 section 11 allows
    # the rules engine's writes) and the owner's read-only reproducibility check, which compares
    # the stored rows with a recompute and prints counts only.
    "ownvitals.derived.findings_store": "the rules engine's writer of findings (#187)",
    "ownvitals.derived.check_cli": "ownvitals analysis check (#187) compares the stored rows",
    # #191: the rules engine's ECG inputs (the Withings fields E1 to E3 read, never the signal or
    # estimated) and the estimated recompute with its sweep (ADR 0006 section 15), both allowed by
    # section 11 by name.
    "ownvitals.derived.findings_ecg": "the rules engine reads the recordings' Withings fields",
    "ownvitals.derived.ecg_recompute": "the estimated recompute and its sweep (§15) write it",
}
# Strings outside ``ALLOWED`` that hold a word and were reviewed as no read: exact, by module,
# with how many times each occurs there.
REVIEWED_STRINGS: dict[tuple[str, str], int] = {
    # A citation of withings-findings §3 in the plausibility table's source column.
    ("ownvitals.analysis.quality", "findings §3: NeuropathyFeature score range 0-100"): 1,
    # The app route a mail links to (the Findings page): the ``UserRoute`` literal, the key of
    # ``ROUTE_PATHS`` and its path.
    ("ownvitals.notify.outbox", "findings"): 2,
    ("ownvitals.notify.outbox", "/findings"): 1,
    # The API's own prefix; the routes read through the read function.
    ("ownvitals.views.findings_routes", "/api/findings"): 1,
}
TABLES = frozenset({"findings", "ecg_recordings"})
_WORD = re.compile(
    r"(?<![A-Za-z0-9_])(findings|estimated|ecg_recordings)(?![A-Za-z0-9_])", re.IGNORECASE
)
_DEFINES = re.compile(
    r"\bcreate\s+(or\s+replace\s+)?((materialized|temp|temporary|recursive)\s+)*"
    r"(view|rule|function|procedure)\b",
    re.IGNORECASE,
)


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every ``str`` constant but docstrings; ``ast.walk`` reaches f-string pieces too."""
    skip = _docstrings(tree)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


def _root(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def scan_source(source: str, module: str) -> list[tuple[int, str]]:
    """What ``module`` does that only the modules of ``ALLOWED`` may."""
    if module in ALLOWED:
        return []
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    strings = _strings(tree)
    for line, value in strings:
        if not _WORD.search(value):
            continue
        pinned = REVIEWED_STRINGS.get((module, value))
        if pinned is None or sum(1 for _, v in strings if v == value) > pinned:
            found.append((line, "a string names findings, estimated or ecg_recordings"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in TABLES:
                    found.append((node.lineno, f"imports {alias.name}"))
                elif alias.name == "*":
                    found.append((node.lineno, "a star import may bind a table"))
                imported.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr in TABLES and _root(node) in imported:
                found.append((node.lineno, f"the {node.attr} table through a module"))
            if (
                node.attr == "estimated"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr in ("c", "columns")
            ):
                found.append((node.lineno, "the estimated column"))
    return sorted(set(found))


def scan_sql(text: str, *, migration: bool) -> list[str]:
    """Database-side second paths: views, rules and functions over the tables (see above)."""
    if not migration:
        return [match.group(0) for match in _WORD.finditer(text)]
    tree = ast.parse(text)
    return [
        value[:60] for _, value in _strings(tree) if _DEFINES.search(value) and _WORD.search(value)
    ]


def _modules() -> list[Path]:
    return sorted((SRC / "ownvitals").rglob("*.py"))


def test_no_module_reads_findings_or_estimated_outside_the_read_function() -> None:
    violations = [
        f"{module_name(path)}:{line}: {why}"
        for path in _modules()
        for line, why in scan_source(path.read_text("utf-8"), module_name(path))
    ]
    assert violations == []


def test_no_view_rule_or_function_over_the_tables() -> None:
    found = [
        f"{path.name}: {hit}"
        for path in sorted(MIGRATIONS.glob("*.py"))
        for hit in scan_sql(path.read_text("utf-8"), migration=True)
    ]
    found += [
        f"{path.relative_to(DOCKER_SQL)}: {hit}"
        for path in sorted(DOCKER_SQL.rglob("*.sql"))
        for hit in scan_sql(path.read_text("utf-8"), migration=False)
    ]
    assert found == []
    assert list(MIGRATIONS.glob("*.py"))  # the control: the scan has files
    assert list(DOCKER_SQL.rglob("*.sql"))


def test_the_allow_list_is_exact() -> None:
    """The reviewed list, and every entry needs its exemption (it names a table today)."""
    assert set(ALLOWED) == {
        "ownvitals.views.findings_read",
        "ownvitals.derived.tables",
        "ownvitals.withings.tables",
        "ownvitals.ingest.rows",
        "ownvitals.ecg_validate",
        "ownvitals.connections.purge",
        "ownvitals.derived.findings_store",
        "ownvitals.derived.check_cli",
        "ownvitals.derived.findings_ecg",
        "ownvitals.derived.ecg_recompute",
    }
    paths = {module_name(path): path for path in _modules()}
    for module in ALLOWED:
        assert scan_source(paths[module].read_text("utf-8"), "ownvitals.elsewhere"), module


def test_every_reviewed_string_is_in_its_module() -> None:
    paths = {module_name(path): path for path in _modules()}
    for (module, value), count in REVIEWED_STRINGS.items():
        tree = ast.parse(paths[module].read_text("utf-8"))
        assert [v for _, v in _strings(tree)].count(value) == count, (module, value)
        assert module not in ALLOWED


# The 25 shapes of the #228 review, and more of the same kinds.
DIRECT_READS = [
    # SQL on findings
    'Q = "SELECT id FROM findings WHERE owner_id = :o"\n',
    'Q = "select * from public.findings"\n',
    'Q = "SELECT * FROM \\"findings\\" AS f"\n',
    'Q = f"SELECT {cols} FROM findings WHERE id = :i"\n',
    'Q = "WITH x AS (SELECT 1) SELECT * FROM x JOIN findings f ON true"\n',
    'Q = "UPDATE findings SET seen_at = now()"\n',
    'Q = "SELECT f.evidence FROM observations o, findings f WHERE o.owner_id = f.owner_id"\n',
    'Q = "SELECT id, evidence " + "FROM findings WHERE owner_id = :o"\n',
    'Q = "SELECT " + cols + " FROM findings"\n',
    'Q = "SELECT * FROM %s" % "findings"\n',
    'Q = "SELECT * FROM {}".format("findings")\n',
    'Q = "SELECT * FROM %(t)s" % {"t": "findings"}\n',
    'Q = "SELECT * FROM FINDINGS"\n',
    # SQL on estimated and ecg_recordings
    'Q = "SELECT estimated FROM ecg_recordings WHERE owner_id = :o"\n',
    'Q = "SELECT r.estimated FROM ecg_recordings AS r"\n',
    "Q = \"SELECT r.estimated->>'hr_bpm' FROM ecg_recordings AS r WHERE r.owner_id = :o\"\n",
    "Q = \"SELECT (estimated#>>'{hrv,rmssd_ms}') FROM x\"\n",
    'Q = "SELECT * FROM ecg_recordings WHERE owner_id = :o"\n',
    'Q = "SELECT r.* FROM ecg_recordings r"\n',
    'Q = "SELECT to_jsonb(r) FROM ecg_recordings r"\n',
    'Q = "SELECT row_to_json(e) FROM public.ecg_recordings e"\n',
    # Core
    "from ownvitals.derived.tables import findings\n",
    "from ownvitals.derived.tables import *\n",
    "from ownvitals.derived import tables\nq = tables.findings\n",
    "from ownvitals.derived import tables as t\nq = t.findings.select()\n",
    "import ownvitals.derived.tables as t\nq = t.findings\n",
    "import ownvitals.derived.tables\nq = ownvitals.derived.tables.findings\n",
    "from ownvitals.connections.purge import findings\n",
    "from ownvitals.connections import purge\nq = purge.ecg_recordings\n",
    "from ownvitals.withings.tables import ecg_recordings\nq = select(ecg_recordings)\n",
    "from ownvitals.withings import tables\nq = select(tables.ecg_recordings)\n",
    "q = recordings.c.estimated\n",
    "q = recordings.columns.estimated\n",
    'q = recordings.columns["estimated"]\n',
    'q = recordings.c["estimated"]\n',
    'q = metadata.tables["findings"]\n',
    'q = metadata.tables.get("ecg_recordings")\n',
    'from sqlalchemy import table, column\nt = table("findings", column("id"))\n',
    'from sqlalchemy import literal_column\nc = literal_column("estimated")\n',
    'q = getattr(tables, "findings")\n',
    'q = Table("ecg_recordings", metadata, autoload_with=conn)\n',
    "from .tables import findings\n",
    "from .. import tables\nq = tables.findings\n",
]


@pytest.mark.parametrize("source", DIRECT_READS)
def test_a_direct_read_in_another_module_is_refused(source: str) -> None:
    for module in ("ownvitals.views.findings_routes", "ownvitals.notify.findings", "ownvitals.x"):
        assert scan_source(source, module), (module, source)


NOT_READS = [
    '"""SELECT * FROM findings: a docstring is prose."""\n',
    'X = "experimental_findings"\n',
    'X = "estimated_total"\n',
    "def f(estimated): return estimated\n",
    "x = record.estimated\n",
    "x = result.findings\n",  # an attribute of a value, not of an imported module
    "findings = []\nfindings.append(1)\n",
    "from ownvitals.derived.tables import TIERS\n",
    'Q = "SELECT * FROM observations WHERE metric_key = :k"\n',
]


@pytest.mark.parametrize("source", NOT_READS)
def test_other_names_pass(source: str) -> None:
    assert scan_source(source, "ownvitals.views.findings_routes") == []


def test_a_reviewed_string_passes_only_in_its_module_and_only_as_often_as_pinned() -> None:
    source = 'R = "/api/findings"\n'
    assert scan_source(source, "ownvitals.views.findings_routes") == []
    assert scan_source(source, "ownvitals.views.other")
    assert scan_source(source + 'T = table("/api/findings")\n', "ownvitals.views.findings_routes")
    outbox = 'K = ("findings", "findings")\n'
    assert scan_source(outbox, "ownvitals.notify.outbox") == []
    assert scan_source(outbox + 'T = table("findings")\n', "ownvitals.notify.outbox")


def test_the_allowed_modules_pass_and_nothing_under_them() -> None:
    sql = 'Q = "SELECT * FROM findings"\n'
    for module in ALLOWED:
        assert scan_source(sql, module) == []
    for module in (f"{READ_FUNCTION}.x", "ownvitals.views", "ownvitals.ingest", "ownvitals.ecg"):
        assert scan_source(sql, module)


@pytest.mark.parametrize(
    "sql",
    [
        'op.execute("CREATE VIEW v AS SELECT * FROM findings")\n',
        'op.execute("CREATE OR REPLACE VIEW v AS SELECT estimated FROM ecg_recordings")\n',
        'op.execute("CREATE MATERIALIZED VIEW v AS SELECT * FROM public.findings")\n',
        'op.execute("CREATE FUNCTION f() RETURNS SETOF findings LANGUAGE sql AS $$ $$")\n',
        'op.execute("CREATE RULE r AS ON SELECT TO x DO INSTEAD SELECT * FROM findings")\n',
    ],
)
def test_a_migration_view_or_function_over_the_tables_is_refused(sql: str) -> None:
    assert scan_sql(sql, migration=True)


def test_migration_tables_and_policies_pass_and_sql_files_are_refused() -> None:
    assert scan_sql('op.execute("CREATE TABLE findings (id uuid)")\n', migration=True) == []
    assert scan_sql('op.execute("CREATE VIEW v AS SELECT * FROM jobs")\n', migration=True) == []
    assert scan_sql("SELECT estimated FROM public.ecg_recordings;", migration=False)
