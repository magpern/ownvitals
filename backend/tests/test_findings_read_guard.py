"""ADR 0006 section 11's guard: findings and ``estimated`` are read for presentation only through
``ownvitals.views.findings_read``, so tier visibility, the experimental switch, the overlay and
RLS cannot be bypassed by a second query path.

``scan_source`` parses one module of ``src/ownvitals`` and returns what it refuses, outside the
modules of ``ALLOWED`` (by name, each with its reason, as section 11 lists them):

- an SQL string (a ``str`` constant, or the constant parts of an f-string, joined) that holds a
  statement verb (``select``, ``insert``, ``update``, ``delete``, ``with``, ``copy``, ``merge``,
  ``truncate``) and names the table ``findings`` after ``from``, ``join``, ``into``, ``update``,
  ``table``, ``using`` or ``truncate`` (optionally ``only``, a ``public.`` qualifier and double
  quotes);
- an SQL string that holds a statement verb, an SQL clause word (``from``, ``set``, ``into``,
  ``join``, ``returning``) and the identifier ``estimated``;
- the Core table: ``findings`` imported from ``ownvitals.derived.tables`` (absolute or relative,
  or ``*``), or reached as an attribute of that module (``tables.findings`` after ``from
  ownvitals.derived import tables``, ``t.findings`` after ``import ownvitals.derived.tables as
  t``, the full chain ``ownvitals.derived.tables.findings``);
- the Core column: ``<x>.c.estimated`` and ``<x>.c["estimated"]``;
- ``table``, ``Table``, ``column`` or ``literal_column`` called with ``"findings"`` or
  ``"estimated"`` as the first argument (or ``name=``).

Docstrings are not scanned. Like the other guards (``session_scan``, ``auth_tables_scan``) it
catches accidental and copied second paths and makes the rest visible in review; it does not stop
obfuscated code (a table name built at run time, ``getattr`` with a computed name, ``exec``).
A new allowed module (the rules engine's findings writer, the ``estimated`` recompute's sweep)
joins ``ALLOWED`` in its own PR, with its reason, and the pinning test changes with it.
"""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
READ_FUNCTION = "ownvitals.views.findings_read"
# ADR 0006 section 11: allowed by name. Writes and definitions, never presentation.
ALLOWED: dict[str, str] = {
    READ_FUNCTION: "the one read function (ADR 0006 section 11)",
    "ownvitals.derived.tables": "defines the findings table; reads nothing",
    "ownvitals.withings.tables": "defines ecg_recordings.estimated; reads nothing",
    "ownvitals.ingest.rows": "ingest and the estimated recompute write estimated (section 15)",
    "ownvitals.ecg_validate": "ownvitals ecg validate (#47) reads the stored estimated (V5)",
    "ownvitals.auth.purge": "the account-delete and purge path",
    "ownvitals.connections.purge": "the purge path (delete imported data)",
}
TABLES_MODULE = "ownvitals.derived.tables"
_VERB = re.compile(r"\b(select|insert|update|delete|with|copy|merge|truncate)\b", re.IGNORECASE)
_FINDINGS_TABLE = re.compile(
    r"\b(from|join|into|update|table|using|truncate)\s+(only\s+)?"
    r"(\"?public\"?\s*\.\s*)?\"?findings\"?(?![\w-])",
    re.IGNORECASE,
)
_CLAUSE = re.compile(r"\b(from|set|into|join|returning)\b", re.IGNORECASE)
_ESTIMATED = re.compile(r"(?<![\w-])\"?estimated\"?(?![\w-])", re.IGNORECASE)
_NAMING_CALLS = frozenset({"table", "Table", "column", "literal_column"})
_NAMES = frozenset({"findings", "estimated"})


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
    """Every string expression but docstrings: constants, and f-strings as their constant
    parts joined (a placeholder becomes a space)."""
    skip = _docstrings(tree)
    inside: set[int] = set()
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                inside.add(id(value))
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append(" ")
            out.append((node.lineno, "".join(parts)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and id(node) not in inside
        ):
            out.append((node.lineno, node.value))
    return out


def _sql_findings(sql: str) -> bool:
    return bool(_VERB.search(sql) and _FINDINGS_TABLE.search(sql))


def _sql_estimated(sql: str) -> bool:
    return bool(_VERB.search(sql) and _CLAUSE.search(sql) and _ESTIMATED.search(sql))


def _absolute(node: ast.ImportFrom, module: str, is_package: bool) -> str:
    if not node.level:
        return node.module or ""
    package = module.split(".") if is_package else module.split(".")[:-1]
    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*base, *([node.module] if node.module else [])])


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return None if inner is None else f"{inner}.{node.attr}"
    return None


def scan_source(source: str, module: str, *, is_package: bool = False) -> list[tuple[int, str]]:
    """What ``module``'s source does that only the read function (or ``ALLOWED``) may."""
    if module in ALLOWED:
        return []
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for line, value in _strings(tree):
        if _sql_findings(value):
            found.append((line, "SQL on the findings table"))
        if _sql_estimated(value):
            found.append((line, "SQL on ecg_recordings.estimated"))
    tables_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source_module = _absolute(node, module, is_package)
            for alias in node.names:
                if source_module == TABLES_MODULE and alias.name in ("findings", "*"):
                    found.append((node.lineno, "imports the findings table"))
                if source_module == "ownvitals.derived" and alias.name == "tables":
                    tables_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == TABLES_MODULE and alias.asname:
                    tables_aliases.add(alias.asname)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            dotted = _dotted(node) or ""
            if node.attr == "findings" and (
                dotted.endswith("derived.tables.findings")
                or (isinstance(node.value, ast.Name) and node.value.id in tables_aliases)
            ):
                found.append((node.lineno, "the findings table"))
            if (
                node.attr == "estimated"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "c"
            ):
                found.append((node.lineno, "the estimated column"))
        elif isinstance(node, ast.Subscript):
            key = node.slice
            if (
                isinstance(node.value, ast.Attribute)
                and node.value.attr == "c"
                and isinstance(key, ast.Constant)
                and key.value == "estimated"
            ):
                found.append((node.lineno, "the estimated column"))
        elif isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            if name in _NAMING_CALLS:
                args = [*node.args[:1], *(k.value for k in node.keywords if k.arg == "name")]
                if any(isinstance(a, ast.Constant) and a.value in _NAMES for a in args):
                    found.append((node.lineno, f"{name}() naming findings or estimated"))
    return sorted(set(found))


def _modules() -> list[Path]:
    return sorted((SRC / "ownvitals").rglob("*.py"))


def test_no_module_reads_findings_or_estimated_outside_the_read_function() -> None:
    violations = [
        f"{module_name(path)}:{line}: {why}"
        for path in _modules()
        for line, why in scan_source(
            path.read_text("utf-8"), module_name(path), is_package=path.name == "__init__.py"
        )
    ]
    assert violations == []


def test_the_allow_list_is_the_reviewed_one_and_every_module_exists() -> None:
    assert set(ALLOWED) == {
        "ownvitals.views.findings_read",
        "ownvitals.derived.tables",
        "ownvitals.withings.tables",
        "ownvitals.ingest.rows",
        "ownvitals.ecg_validate",
        "ownvitals.auth.purge",
        "ownvitals.connections.purge",
    }
    known = {module_name(path) for path in _modules()}
    assert set(ALLOWED) <= known


def test_the_read_function_and_the_allowed_writers_do_what_the_guard_looks_for() -> None:
    """The control: without its exemption each module that reads or writes these rows today is
    flagged, so the patterns match the real statements."""
    for module in (READ_FUNCTION, "ownvitals.ingest.rows", "ownvitals.ecg_validate"):
        path = SRC / Path(*module.split(".")).with_suffix(".py")
        assert scan_source(path.read_text("utf-8"), "ownvitals.elsewhere"), module


DIRECT_READS = [
    'from sqlalchemy import text\nQ = text("SELECT id FROM findings WHERE owner_id = :o")\n',
    'Q = "select * from public.findings"\n',
    'Q = "SELECT * FROM \\"findings\\" AS f"\n',
    'Q = f"SELECT {cols} FROM findings WHERE id = :i"\n',
    'Q = "WITH x AS (SELECT 1) SELECT * FROM x JOIN findings f ON true"\n',
    'Q = "UPDATE findings SET seen_at = now()"\n',
    'Q = "SELECT estimated FROM ecg_recordings WHERE owner_id = :o"\n',
    'Q = "SELECT r.estimated FROM ecg_recordings AS r"\n',
    "from ownvitals.derived.tables import findings\n",
    "from ownvitals.derived.tables import *\n",
    "from ownvitals.derived import tables\nq = tables.findings\n",
    "from ownvitals.derived import tables as t\nq = t.findings.select()\n",
    "import ownvitals.derived.tables as t\nq = t.findings\n",
    "import ownvitals.derived.tables\nq = ownvitals.derived.tables.findings\n",
    "from ownvitals.withings.tables import ecg_recordings\nq = ecg_recordings.c.estimated\n",
    'q = ecg_recordings.c["estimated"]\n',
    'from sqlalchemy import table, column\nt = table("findings", column("id"))\n',
    'from sqlalchemy import literal_column\nc = literal_column("estimated")\n',
]


@pytest.mark.parametrize("source", DIRECT_READS)
def test_a_direct_read_in_another_module_is_refused(source: str) -> None:
    for module in ("ownvitals.views.findings_routes", "ownvitals.notify.findings", "ownvitals.x"):
        assert scan_source(source, module), (module, source)


def test_a_relative_import_of_the_table_is_refused() -> None:
    source = "from ..derived.tables import findings\n"
    assert scan_source(source, "ownvitals.views.other")
    assert scan_source("from .tables import findings\n", "ownvitals.derived.other")
    assert scan_source("from . import tables\nq = tables.findings\n", "ownvitals.derived.x")


NOT_READS = [
    '"""SELECT * FROM findings: a docstring is prose."""\n',
    'X = "withings-findings §2: the real value"\n',
    'X = "findings §3: NeuropathyFeature score range"\n',
    'route = {"findings": "/findings"}\n',
    'X = "experimental_findings"\n',
    'X = "the estimated value of the trend"\n',
    "def f(estimated): return estimated\n",
    "x = record.estimated\n",
    "from ownvitals.derived.tables import TIERS\n",
    'Q = "SELECT * FROM findings_archive"\n',
    'Q = "SELECT * FROM observations WHERE metric_key = :k"\n',
]


@pytest.mark.parametrize("source", NOT_READS)
def test_prose_and_other_tables_pass(source: str) -> None:
    assert scan_source(source, "ownvitals.views.findings_routes") == []


def test_the_allowed_modules_pass_and_nothing_under_them() -> None:
    sql = 'Q = "SELECT * FROM findings"\n'
    for module in ALLOWED:
        assert scan_source(sql, module) == []
    for module in (f"{READ_FUNCTION}.x", "ownvitals.views", "ownvitals.ingest", "ownvitals.ecg"):
        assert scan_source(sql, module)
