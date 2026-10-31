"""Purity guard: ``ownvitals.analysis`` and ``ownvitals.ecg`` are pure libraries.

They import nothing from ``ownvitals.db``, ``web``, ``withings``, ``jobs`` or ``egress`` and
nothing from ``httpx``, so they can be tested, reasoned about and run without a database, a
web app or a network. Two checks:

* static: every import in the package's source (absolute, relative, ``from ownvitals import db``,
  ``importlib.import_module`` and ``__import__`` with a literal) is read from the syntax tree.
  The scanner fails closed, so these count as violations as well: an import call whose target is
  not a literal string, that starts with ``.``, that passes a second positional argument,
  ``package=``, ``level=`` or ``**kwargs``, or whose ``fromlist`` names a forbidden module or is
  not a literal; a reference to ``import_module``/``__import__`` that is not a direct call (an
  alias such as ``from importlib import import_module as load``); and any use of ``exec``,
  ``eval`` or ``compile``, which can run an import that no syntax tree shows;
* dynamic: importing every module of the package in a clean interpreter must not load any of
  the forbidden modules, so an indirect import through a helper is caught as well.

The static scanner is itself tested by planting forbidden imports into a copy of the package,
in a module of the package and in one of its ``methods/`` subpackage. A guarded name must be a
package: ``ownvitals/ecg.py`` fails the guard instead of being skipped.
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
    "httpx",
)
IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})
DYNAMIC_CODE = frozenset({"exec", "eval", "compile"})  # run text as code: an import nobody sees


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


def import_aliases(node: ast.Import) -> dict[str, str]:
    """Local name -> module, for ``import ownvitals[.x] [as y]``."""
    aliases: dict[str, str] = {}
    for alias in node.names:
        head = alias.name.split(".")[0]
        aliases[alias.asname or head] = alias.name if alias.asname else head
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
        if alias.name in IMPORT_FUNCTIONS | DYNAMIC_CODE and alias.asname not in (None, alias.name)
    )
    return problems


def reference_problems(
    node: ast.Name | ast.Attribute, aliases: dict[str, str], called: set[int]
) -> list[str]:
    """Names that reach imports: ``exec``/``eval``/``compile``, and ``import_module`` or
    ``__import__`` used as a value (``load = importlib.import_module``), not called directly."""
    name = node.id if isinstance(node, ast.Name) else node.attr
    problems: list[str] = []
    if name in IMPORT_FUNCTIONS and id(node) not in called:
        problems.append(f"reference to {name} that is not a direct call")
    if isinstance(node, ast.Name):
        if name in DYNAMIC_CODE:
            problems.append(f"use of {name}")
        return problems
    chain = dotted(node)
    head, _, rest = (chain or "").partition(".")
    if rest and head in aliases and is_forbidden(f"{aliases[head]}.{rest}"):
        problems.append(f"attribute access {chain}")
    if name in DYNAMIC_CODE and aliases.get(head) == "builtins":
        problems.append(f"use of builtins.{name}")
    return problems


def violations_in(path: Path, root: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(root).as_posix()
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    aliases: dict[str, str] = {}
    found: list[str] = []
    for node in ast.walk(tree):
        problems: list[str] = []
        if isinstance(node, ast.Import | ast.ImportFrom):
            problems = import_problems(node, path, root)
            if isinstance(node, ast.Import):
                aliases.update(import_aliases(node))
        elif isinstance(node, ast.Call):
            if (dotted(node.func) or "").split(".")[-1] in IMPORT_FUNCTIONS:
                problems = import_call_problems(node)
        elif isinstance(node, ast.Name | ast.Attribute):
            problems = reference_problems(node, aliases, called)
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
        "from ownvitals.config import Settings",  # not on the list
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


# --- the ecg checks cannot be skipped by a module ---------------------------------------------


def ownvitals_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "ownvitals"
    directory.mkdir()
    return directory


def never_skipped(name: str, src: Path) -> Path:
    """``present`` for a package that exists: a skip here is a failure, not a silent pass."""
    try:
        return present(name, src)
    except pytest.skip.Exception:
        pytest.fail(f"ownvitals.{name} exists but the purity guard skipped it")


def test_the_real_analysis_package_is_never_skipped() -> None:
    assert never_skipped("analysis", SRC) == SRC / "ownvitals" / "analysis"


def test_a_guarded_name_that_does_not_exist_yet_is_skipped(tmp_path: Path) -> None:
    ownvitals_dir(tmp_path)
    with pytest.raises(pytest.skip.Exception, match=r"ownvitals\.ecg does not exist yet"):
        present("ecg", tmp_path)


@pytest.mark.parametrize("with_init", [True, False])
def test_an_ecg_package_is_scanned_not_skipped(tmp_path: Path, with_init: bool) -> None:
    package = ownvitals_dir(tmp_path) / "ecg"
    package.mkdir()
    if with_init:
        (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text("from ownvitals.db import engine\n", encoding="utf-8")
    assert never_skipped("ecg", tmp_path) == package
    assert violations(package, tmp_path) == [
        "ownvitals/ecg/core.py:1: from ownvitals.db import engine"
    ]


def test_an_ecg_module_fails_the_guard_instead_of_being_skipped(tmp_path: Path) -> None:
    (ownvitals_dir(tmp_path) / "ecg.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match=r"ecg\.py exists.*must be a package"):
        present("ecg", tmp_path)


def test_an_ecg_module_next_to_an_ecg_package_still_fails(tmp_path: Path) -> None:
    directory = ownvitals_dir(tmp_path)
    (directory / "ecg").mkdir()
    (directory / "ecg.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="must be a package"):
        present("ecg", tmp_path)


def test_an_analysis_module_cannot_replace_the_analysis_package_either(tmp_path: Path) -> None:
    (ownvitals_dir(tmp_path) / "analysis.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match="must be a package"):
        present("analysis", tmp_path)
