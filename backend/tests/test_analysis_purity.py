"""Purity guard: ``ownvitals.analysis`` and ``ownvitals.ecg`` are pure libraries.

They import nothing from ``ownvitals.db``, ``web``, ``withings``, ``jobs`` or ``egress`` and
nothing from ``httpx``, so they can be tested, reasoned about and run without a database, a
web app or a network. Two checks:

* static: every import in the package's source (absolute, relative, ``from ownvitals import db``,
  ``importlib.import_module`` and ``__import__`` with a literal) is read from the syntax tree;
  an import whose target is not a literal string counts as a violation too;
* dynamic: importing every module of the package in a clean interpreter must not load any of
  the forbidden modules, so an indirect import through a helper is caught as well.

The static scanner is itself tested by planting forbidden imports into a copy of the package.
"""

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
GUARDED = ("analysis", "ecg")  # ownvitals.ecg arrives with the ECG core issue
FORBIDDEN = (
    "ownvitals.db",
    "ownvitals.web",
    "ownvitals.withings",
    "ownvitals.jobs",
    "ownvitals.egress",
    "httpx",
)


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


def violations_in(path: Path, root: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(root).as_posix()
    found: list[str] = []
    aliases: dict[str, str] = {}  # local name -> module, for `import ownvitals[.x] [as y]`
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_forbidden(alias.name):
                    found.append(f"{where}:{node.lineno}: import {alias.name}")
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            base = absolute_from(node, path, root)
            for alias in node.names:
                target = f"{base}.{alias.name}" if base else alias.name
                if is_forbidden(base) or is_forbidden(target):
                    found.append(f"{where}:{node.lineno}: from {base} import {alias.name}")
        elif isinstance(node, ast.Call):
            callee = dotted(node.func) or ""
            if callee.split(".")[-1] in {"import_module", "__import__"}:
                first = node.args[0] if node.args else None
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    found.append(f"{where}:{node.lineno}: dynamic import of a non-literal")
                elif is_forbidden(first.value):
                    found.append(f"{where}:{node.lineno}: dynamic import of {first.value}")
        elif isinstance(node, ast.Attribute):
            chain = dotted(node)
            head, _, rest = (chain or "").partition(".")
            if rest and head in aliases and is_forbidden(f"{aliases[head]}.{rest}"):
                found.append(f"{where}:{node.lineno}: attribute access {chain}")
    return found


def violations(package_dir: Path, root: Path) -> list[str]:
    return [v for path in sorted(package_dir.rglob("*.py")) for v in violations_in(path, root)]


def present(name: str) -> Path:
    directory = SRC / "ownvitals" / name
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
    modules = sorted(module_name(p, SRC) for p in directory.rglob("*.py"))
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
    methods.mkdir()
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
    ],
)
def test_allowed_imports_are_not_reported(tmp_path: Path, line: str) -> None:
    target = planted_copy(tmp_path, line)
    assert violations(target, tmp_path) == [], line
