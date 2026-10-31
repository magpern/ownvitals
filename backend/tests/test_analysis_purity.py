"""Purity guard: ``ownvitals.analysis`` and ``ownvitals.ecg`` are pure libraries.

They import nothing that does I/O or knows who is acting (``FORBIDDEN``: the database, web,
Withings, jobs, egress, accounts, connections, context, configuration and logging modules of
``ownvitals``, its entry point ``ownvitals.__main__``, and ``sqlalchemy``, ``psycopg``, ``httpx``
and ``requests``), so they can be
tested, reasoned about and run without a database, a web app or a network. Two checks:

* static: every import in the package's source (absolute, relative, ``from ownvitals import db``,
  ``importlib.import_module`` and ``__import__`` with a literal) is read from the syntax tree.
  The scanner fails closed, so these count as violations as well: an import call whose target is
  not a literal string, that starts with ``.``, that passes a second positional argument,
  ``package=``, ``level=`` or ``**kwargs``, or whose ``fromlist`` names a forbidden module or is
  not a literal; a reference to ``import_module``/``__import__`` that is not a direct call (an
  alias such as ``from importlib import import_module as load``); any use of ``exec``, ``eval``
  or ``compile``, which can run an import that no syntax tree shows; any use of ``run_module``,
  ``run_path``, ``resolve_name`` or ``patch``, which import or run what a string names; and a
  string that is exactly the name of one of these (``getattr(importlib, 'import_module')``). An
  attribute call counts whatever its base: ``__import__('importlib').import_module(...)`` and
  ``sys.modules['importlib'].import_module(...)`` are import calls. Import aliases are collected
  from the whole file before anything is checked, so the order of the code does not matter. Names
  built at run time (``getattr(m, 'import_' + 'module')``) are beyond a syntax tree; the dynamic
  check below is the backstop;
* dynamic: importing every module of the package in a clean interpreter must not load any of
  the forbidden modules, so an indirect import through a helper is caught as well.

The static scanner is itself tested by planting forbidden imports into a copy of the package,
in a module of the package and in one of its ``methods/`` subpackage. A guarded name must be a
package: ``ownvitals/ecg.py`` fails the guard instead of being skipped. The rules added in #76 are
tested in ``tests/analysis/test_analysis_purity_rules.py`` and the skip checks in
``tests/analysis/test_analysis_purity_guarded.py`` (split out to keep this file under 500 lines).
"""

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
GUARDED = ("analysis", "ecg")  # ownvitals.ecg arrives with the ECG core issue (#40)
FORBIDDEN = (
    "ownvitals.db",
    "ownvitals.web",
    "ownvitals.withings",
    "ownvitals.jobs",
    "ownvitals.egress",
    "ownvitals.context",  # who is acting: the pure core takes plain values
    "ownvitals.auth",
    "ownvitals.connections",
    "ownvitals.admin",
    "ownvitals.flags",
    "ownvitals.cli",
    "ownvitals.config",  # reads the environment and secret files
    "ownvitals.log",
    "ownvitals.headers",
    "ownvitals.__main__",  # the entry point: uvicorn, the web app, configuration and logging
    "httpx",
    "requests",
    "sqlalchemy",
    "psycopg",
)
IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})
DYNAMIC_CODE = frozenset({"exec", "eval", "compile"})  # run text as code: an import nobody sees
# Import or run whatever module a string names (``patch`` imports its target, also in
# ``patch.dict`` and ``patch.multiple``). A pure library has no use for them.
STRING_IMPORTERS = frozenset({"run_module", "run_path", "resolve_name", "patch"})
CODE_LOADERS = IMPORT_FUNCTIONS | DYNAMIC_CODE | STRING_IMPORTERS


def is_forbidden(module: str) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in FORBIDDEN)


def module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def absolute_from(node: ast.ImportFrom, path: Path, root: Path) -> str:
    """The dotted module an ``ImportFrom`` names, resolving relative imports."""
    if node.level == 0:
        return node.module or ""
    package = module_name(path, root).split(".")
    if path.name != "__init__.py":
        package.pop()
    if node.level > 1:
        package = package[: len(package) - (node.level - 1)]
    return ".".join([*package, *([node.module] if node.module else [])])


def dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def callee_name(func: ast.expr) -> str | None:
    """The name a call is made under: ``f`` for ``f()`` and for ``x.f()``, whatever ``x`` is (a
    name, a call, a subscript)."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def literal(node: ast.expr | None) -> str | None:
    """The value of a string literal, or ``None`` for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def fromlist_problems(module: str, node: ast.expr) -> list[str]:
    """What is wrong with the ``fromlist`` of ``__import__(module, fromlist=...)``."""
    if isinstance(node, ast.Constant) and node.value is None:
        return []
    if not isinstance(node, ast.List | ast.Tuple):
        return ["dynamic import with a non-literal fromlist"]
    problems: list[str] = []
    for element in node.elts:
        item = literal(element)
        if item is None:
            problems.append("dynamic import with a non-literal fromlist")
        elif is_forbidden(item) or is_forbidden(f"{module}.{item}"):
            problems.append(f"dynamic import with a fromlist naming {module}.{item}")
    return problems


def import_call_problems(node: ast.Call) -> list[str]:
    """What is wrong with a call of ``import_module`` or ``__import__``: a target that is not a
    forbidden literal, and every way to reach a module other than by that literal name."""
    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg is not None}
    problems: list[str] = []
    target = literal(node.args[0] if node.args else keywords.get("name"))
    if target is None:
        problems.append("dynamic import of a non-literal")
    elif target.startswith("."):
        problems.append(f"relative dynamic import of {target}")
    elif is_forbidden(target):
        problems.append(f"dynamic import of {target}")
    if len(node.args) > 1:  # package, or globals, locals, fromlist and level of __import__
        problems.append("dynamic import with a second positional argument")
    if any(kw.arg is None for kw in node.keywords):
        problems.append("dynamic import with **kwargs")
    problems.extend(
        f"dynamic import with {name}=" for name in ("package", "level") if name in keywords
    )
    if "fromlist" in keywords:
        problems.extend(fromlist_problems(target or "", keywords["fromlist"]))
    return problems


def import_aliases(tree: ast.AST) -> dict[str, set[str]]:
    """Local name -> every module bound to it by ``import ownvitals[.x] [as y]`` anywhere in the
    file. Taken before anything is checked (``ast.walk`` goes by depth, not by source order, and
    a use can precede an import nested deeper), and a name bound twice keeps both modules."""
    aliases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                bound = alias.name if alias.asname else head
                aliases.setdefault(alias.asname or head, set()).add(bound)
    return aliases


def import_problems(node: ast.Import | ast.ImportFrom, path: Path, root: Path) -> list[str]:
    if isinstance(node, ast.Import):
        return [f"import {a.name}" for a in node.names if is_forbidden(a.name)]
    base = absolute_from(node, path, root)
    problems = [
        f"from {base} import {alias.name}"
        for alias in node.names
        if is_forbidden(base) or is_forbidden(f"{base}.{alias.name}" if base else alias.name)
    ]
    # `from importlib import import_module as load`: the calls of `load` are not recognisable
    problems.extend(
        f"renamed import of {alias.name}"
        for alias in node.names
        if alias.name in CODE_LOADERS and alias.asname not in (None, alias.name)
    )
    return problems


def reference_problems(
    node: ast.Name | ast.Attribute, aliases: dict[str, set[str]], called: set[int]
) -> list[str]:
    """Names that reach imports: ``exec``/``eval``/``compile``, ``run_module``, ``run_path``,
    ``resolve_name`` and ``patch``, and ``import_module`` or ``__import__`` used as a value
    (``load = importlib.import_module``), not called directly."""
    name = node.id if isinstance(node, ast.Name) else node.attr
    problems: list[str] = []
    if name in IMPORT_FUNCTIONS and id(node) not in called:
        problems.append(f"reference to {name} that is not a direct call")
    if name in STRING_IMPORTERS:
        problems.append(f"use of {name}, which imports or runs a module by name")
    if isinstance(node, ast.Name):
        if name in DYNAMIC_CODE:
            problems.append(f"use of {name}")
        return problems
    chain = dotted(node)
    head, _, rest = (chain or "").partition(".")
    modules = aliases.get(head, set())
    if rest and any(is_forbidden(f"{module}.{rest}") for module in modules):
        problems.append(f"attribute access {chain}")
    # `re.compile` is fine; `builtins.exec` and `__import__('builtins').exec` (a base that is
    # not a plain name) are not
    if name in DYNAMIC_CODE and ("builtins" in modules or chain is None):
        problems.append(f"use of builtins.{name}")
    return problems


def violations_in(path: Path, root: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(root).as_posix()
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    aliases = import_aliases(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        problems: list[str] = []
        if isinstance(node, ast.Import | ast.ImportFrom):
            problems = import_problems(node, path, root)
        elif isinstance(node, ast.Call):
            if callee_name(node.func) in IMPORT_FUNCTIONS:
                problems = import_call_problems(node)
        elif isinstance(node, ast.Name | ast.Attribute):
            problems = reference_problems(node, aliases, called)
        elif isinstance(node, ast.Constant) and node.value in CODE_LOADERS:
            # getattr(importlib, 'import_module'), vars(importlib)['import_module'], ...
            problems = [f"lookup by name of {node.value}"]
        found.extend(f"{where}:{getattr(node, 'lineno', 0)}: {p}" for p in problems)
    return found


def source_files(package_dir: Path) -> list[Path]:
    """Every module of the package, ``methods/`` and any other subpackage included."""
    return sorted(package_dir.rglob("*.py"))


def violations(package_dir: Path, root: Path) -> list[str]:
    return [v for path in source_files(package_dir) for v in violations_in(path, root)]


def present(name: str, src: Path = SRC) -> Path:
    """The guarded package ``ownvitals.<name>``. It is skipped only while neither ``<name>/`` nor
    ``<name>.py`` exists; a module cannot be scanned as a package, so it fails the guard."""
    module = src / "ownvitals" / f"{name}.py"
    directory = src / "ownvitals" / name
    if module.exists():
        pytest.fail(
            f"ownvitals/{name}.py exists: ownvitals.{name} must be a package (ownvitals/{name}/) "
            "so that the purity guard scans all of it"
        )
    if not directory.is_dir():
        pytest.skip(f"ownvitals.{name} does not exist yet")
    return directory


# --- the real packages ------------------------------------------------------------------------


def test_the_analysis_package_exists_so_the_guard_is_not_vacuous() -> None:
    modules = {p.stem for p in (SRC / "ownvitals" / "analysis").glob("*.py")}
    assert {"quality", "daily", "policy", "dataquality"} <= modules


@pytest.mark.parametrize("name", GUARDED)
def test_the_source_imports_nothing_forbidden(name: str) -> None:
    assert violations(present(name), SRC) == []


@pytest.mark.parametrize("name", GUARDED)
def test_importing_every_module_loads_nothing_forbidden(name: str) -> None:
    directory = present(name)
    modules = sorted(module_name(p, SRC) for p in source_files(directory))
    code = (
        "import importlib, sys\n"
        f"for name in {modules!r}:\n"
        "    importlib.import_module(name)\n"
        f"loaded = [m for m in sys.modules if m in {FORBIDDEN!r} "
        f"or any(m.startswith(f + '.') for f in {FORBIDDEN!r})]\n"
        "print(','.join(sorted(loaded)))\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed argument list, no shell, no untrusted input
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "", f"importing ownvitals.{name} loaded {done.stdout.strip()}"


# --- the scanner itself: planted imports are caught ------------------------------------------


def planted_copy(tmp_path: Path, line: str, filename: str = "quality.py") -> Path:
    """A copy of the real analysis package with ``line`` appended to one of its modules."""
    target = tmp_path / "ownvitals" / "analysis"
    shutil.copytree(
        SRC / "ownvitals" / "analysis", target, ignore=shutil.ignore_patterns("__pycache__")
    )
    module = target / filename
    module.write_text(module.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")
    return target


def test_the_real_package_copy_is_clean_before_anything_is_planted(tmp_path: Path) -> None:
    target = tmp_path / "ownvitals" / "analysis"
    shutil.copytree(
        SRC / "ownvitals" / "analysis", target, ignore=shutil.ignore_patterns("__pycache__")
    )
    assert violations(target, tmp_path) == []


@pytest.mark.parametrize(
    "line",
    [
        "import ownvitals.db",
        "import ownvitals.db.engine",
        "import ownvitals.db as database",
        "from ownvitals.db import engine",
        "from ownvitals.db.engine import make_engine",
        "from ownvitals import db",
        "from ownvitals import web",
        "from ownvitals.web import create_app",
        "from ownvitals.withings import client",
        "import ownvitals.jobs",
        "from ownvitals.egress import allowlist",
        "from .. import db",
        "from ..db import engine",
        "from ..web import create_app",
        "import httpx",
        "import httpx as h",
        "from httpx import Client",
        "import importlib\nimportlib.import_module('ownvitals.db')",
        "__import__('httpx')",
        "from importlib import import_module\nimport_module('ownvitals.web.app')",
        "import importlib\nimportlib.import_module(some_name)",
        "import ownvitals\nownvitals.db.engine",
        "import ownvitals as ov\nov.withings",
        "def late():\n    from ownvitals.db import engine\n    return engine",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ownvitals.db import engine",
    ],
)
def test_a_planted_forbidden_import_fails_the_guard(tmp_path: Path, line: str) -> None:
    target = planted_copy(tmp_path, line)
    assert violations(target, tmp_path), line


def test_a_planted_import_in_a_subpackage_fails_the_guard(tmp_path: Path) -> None:
    target = planted_copy(tmp_path, "x = 1", filename="quality.py")
    methods = target / "methods"
    methods.mkdir(exist_ok=True)  # the real package has analysis/methods/ since #38
    (methods / "__init__.py").write_text("", encoding="utf-8")
    (methods / "trend.py").write_text("from ...db import engine\n", encoding="utf-8")
    found = violations(target, tmp_path)
    assert found
    assert "methods/trend.py" in found[0]


@pytest.mark.parametrize(
    "line",
    [
        "import math",
        "from decimal import Decimal",
        "from ownvitals.analysis import quality",
        "from . import quality",
        "from .quality import Reading",
        "from ownvitals.emailnorm import normalize_email",  # not on the list
        "from ownvitals import emailnorm",
        "import httpx_not_really",
        "from ownvitals.dbtools import x",
        "import ownvitals\nownvitals.__version__",
        "import numpy as np",
        "import re\nPATTERN = re.compile('[a-z]+')",  # an attribute `compile` is not the builtin
        "import importlib\nimportlib.import_module('math')",
        "import importlib\nimportlib.import_module('ownvitals.analysis.quality')",
        "from importlib import import_module\nimport_module('decimal')",
        "__import__('math', fromlist=['sqrt'])",
        "__import__('math', fromlist=None)",
        "executor = evaluate = 'exec eval compile'",
    ],
)
def test_allowed_imports_are_not_reported(tmp_path: Path, line: str) -> None:
    target = planted_copy(tmp_path, line)
    assert violations(target, tmp_path) == [], line


# --- the import forms a plain `import` scan misses --------------------------------------------
# Each line is planted on its own; the report must contain the fragment of the rule that is
# meant to catch it, so dropping one rule fails exactly its tests.

NEWER_FORMS = [
    # a relative target is resolved against a package the scanner cannot see
    ("import importlib\nimportlib.import_module('.db', package='ownvitals')", "relative dynamic"),
    (
        "import importlib\nimportlib.import_module('..db', package='ownvitals.x')",
        "relative dynamic",
    ),
    (
        "import importlib\nimportlib.import_module('.quality')",
        "relative dynamic import of .quality",
    ),
    ("__import__('..db')", "relative dynamic import of ..db"),
    # package, level and a second positional argument (package, or globals/locals/fromlist/level)
    ("import importlib\nimportlib.import_module('db', package='ownvitals')", "with package="),
    ("__import__('db', level=1)", "with level="),
    ("import importlib\nimportlib.import_module('db', 'ownvitals')", "second positional"),
    ("__import__('ownvitals', globals(), locals(), ['db'], 0)", "second positional"),
    ("import importlib\nimportlib.import_module('math', **options)", "**kwargs"),
    ("import importlib\nimportlib.import_module(name='ownvitals.db')", "import of ownvitals.db"),
    # fromlist
    ("__import__('ownvitals', fromlist=['db'])", "fromlist naming ownvitals.db"),
    ("__import__('ownvitals', fromlist=('analysis', 'web'))", "fromlist naming ownvitals.web"),
    ("__import__('ownvitals', fromlist=['withings.client'])", "ownvitals.withings.client"),
    ("__import__('x', fromlist=['httpx'])", "fromlist naming x.httpx"),
    ("import builtins\nbuiltins.__import__('ownvitals', fromlist=['jobs'])", "ownvitals.jobs"),
    ("__import__('ownvitals', fromlist=names)", "non-literal fromlist"),
    ("__import__('ownvitals', fromlist=[name])", "non-literal fromlist"),
    # exec, eval and compile can run an import that no syntax tree shows
    ("exec('import ownvitals.db')", "use of exec"),
    ("eval(\"__import__('httpx')\")", "use of eval"),
    ("compile('import httpx', '<planted>', 'exec')", "use of compile"),
    ("run = exec", "use of exec"),
    ("import builtins\nbuiltins.exec('import httpx')", "use of builtins.exec"),
    ("import builtins as b\nb.eval('1')", "use of builtins.eval"),
    ("import builtins\nbuiltins.compile('1', 's', 'eval')", "use of builtins.compile"),
    # an alias hides the call from the scan
    (
        "from importlib import import_module as load\nload('ownvitals.db')",
        "renamed import of import_module",
    ),
    ("from builtins import __import__ as load\nload('httpx')", "renamed import of __import__"),
    ("from builtins import exec as run\nrun('import httpx')", "renamed import of exec"),
    (
        "import importlib\nload = importlib.import_module\nload('ownvitals.db')",
        "not a direct call",
    ),
    ("import importlib\nlist(map(importlib.import_module, ['ownvitals.db']))", "not a direct call"),
]


@pytest.mark.parametrize(("line", "fragment"), NEWER_FORMS)
def test_each_newer_import_form_fails_the_guard(tmp_path: Path, line: str, fragment: str) -> None:
    found = violations(planted_copy(tmp_path, line), tmp_path)
    assert any(fragment in v for v in found), (fragment, found)


@pytest.mark.parametrize(("line", "fragment"), NEWER_FORMS)
def test_each_newer_import_form_fails_the_guard_in_the_methods_subpackage(
    tmp_path: Path, line: str, fragment: str
) -> None:
    target = planted_copy(tmp_path, line, filename="methods/trend.py")
    found = violations(target, tmp_path)
    assert any(fragment in v for v in found), (fragment, found)
    assert all(v.startswith("ownvitals/analysis/methods/trend.py:") for v in found), found


def test_the_scan_covers_the_methods_subpackage() -> None:
    scanned = {p.relative_to(SRC).as_posix() for p in source_files(present("analysis"))}
    methods = {"baseline", "changepoint", "ewma", "series", "trend"}
    assert {f"ownvitals/analysis/methods/{name}.py" for name in methods} <= scanned
