"""The purity guard's rules from the #74 review (#76), each planted into a copy of the package.

A rule is tested with a line that only that rule reports, and the report must contain the
fragment the rule writes, so dropping one rule fails exactly its tests. The same lines are planted
in ``quality.py`` and in ``methods/trend.py``, and each ``FORBIDDEN`` addition is listed here by
name, not read from ``FORBIDDEN``, so removing an entry fails a test.
"""

from pathlib import Path

import pytest

from test_analysis_purity import planted_copy, violations


def found_in(tmp_path: Path, line: str, filename: str = "quality.py") -> list[str]:
    return violations(planted_copy(tmp_path, line, filename), tmp_path)


# --- a call is an import call by the name it is made under, whatever its base ------------------
# `__import__('importlib').import_module(...)` has a call for a base, `sys.modules[...]` a
# subscript: neither is a plain dotted name.

CALL_CHAINS = [
    ("__import__('importlib').import_module('ownvitals.db')", "dynamic import of ownvitals.db"),
    (
        "import sys\nsys.modules['importlib'].import_module('ownvitals.db')",
        "dynamic import of ownvitals.db",
    ),
    ("__import__('importlib').import_module(name)", "dynamic import of a non-literal"),
    ("__import__('importlib').import_module('.db', 'ownvitals')", "relative dynamic import of .db"),
    ("import sys\nsys.modules['builtins'].__import__('httpx')", "dynamic import of httpx"),
    ("loader().import_module('httpx')", "dynamic import of httpx"),
    # the same for exec, eval and compile; `re.compile` stays allowed (see below)
    ("__import__('builtins').exec('import httpx')", "use of builtins.exec"),
    ("import sys\nsys.modules['builtins'].eval('1')", "use of builtins.eval"),
    ("__import__('builtins').compile('1', 's', 'eval')", "use of builtins.compile"),
]

# --- an import function looked up by its name as a string --------------------------------------

NAME_LOOKUPS = [
    ("import importlib\ngetattr(importlib, 'import_module')", "lookup by name of import_module"),
    (
        "import importlib\ngetattr(importlib, 'import_module')('ownvitals.db')",
        "lookup by name of import_module",
    ),
    ("import builtins\ngetattr(builtins, '__import__')('httpx')", "lookup by name of __import__"),
    (
        "import importlib\nimportlib.__dict__['import_module']('ownvitals.db')",
        "lookup by name of import_module",
    ),
    (
        "import importlib\nvars(importlib)['import_module']('ownvitals.db')",
        "lookup by name of import_module",
    ),
    ("import builtins\ngetattr(builtins, 'exec')('import httpx')", "lookup by name of exec"),
    ("import runpy\ngetattr(runpy, 'run_module')", "lookup by name of run_module"),
    ("import pkgutil\ngetattr(pkgutil, 'resolve_name')", "lookup by name of resolve_name"),
    ("from unittest import mock\ngetattr(mock, 'patch')", "lookup by name of patch"),
]

# --- functions that import or run the module a string names ------------------------------------
# Not forbidden by their target alone: `pkgutil.resolve_name('ownvitals.db:engine')` has a colon
# that `is_forbidden` does not see, and a patch target names an attribute, not a module.

STRING_IMPORTERS = [
    ("import runpy\nrunpy.run_module('ownvitals.db')", "use of run_module"),
    ("import runpy\nrunpy.run_path('somewhere.py')", "use of run_path"),
    ("from runpy import run_module\nrun_module('httpx')", "use of run_module"),
    ("import pkgutil\npkgutil.resolve_name('ownvitals.db:engine')", "use of resolve_name"),
    ("from pkgutil import resolve_name\nresolve_name('httpx:Client')", "use of resolve_name"),
    ("from unittest.mock import patch\npatch('ownvitals.db.engine.make')", "use of patch"),
    ("import unittest.mock\nunittest.mock.patch('httpx.Client')", "use of patch"),
    ("from unittest import mock\nmock.patch('ownvitals.web.app')", "use of patch"),
    ("from unittest import mock\nmock.patch.dict('os.environ', {})", "use of patch"),
    ("from unittest import mock\nmock.patch.multiple('ownvitals.db', engine=None)", "use of patch"),
    # under another name the call is not recognisable, so the import itself is refused
    ("from runpy import run_path as go\ngo('somewhere.py')", "renamed import of run_path"),
    ("from pkgutil import resolve_name as find\nfind('httpx:Client')", "renamed import of resolve"),
    ("from unittest.mock import patch as swap\nswap('ownvitals.db.x')", "renamed import of patch"),
]

PLANTED = CALL_CHAINS + NAME_LOOKUPS + STRING_IMPORTERS


@pytest.mark.parametrize(("line", "fragment"), PLANTED)
def test_each_rule_fails_the_guard(tmp_path: Path, line: str, fragment: str) -> None:
    found = found_in(tmp_path, line)
    assert any(fragment in v for v in found), (fragment, found)


@pytest.mark.parametrize(("line", "fragment"), PLANTED)
def test_each_rule_fails_the_guard_in_the_methods_subpackage(
    tmp_path: Path, line: str, fragment: str
) -> None:
    found = found_in(tmp_path, line, "methods/trend.py")
    assert any(fragment in v for v in found), (fragment, found)
    assert all(v.startswith("ownvitals/analysis/methods/trend.py:") for v in found), found


@pytest.mark.parametrize(
    "line",
    [
        "import re\nPATTERN = re.compile('[a-z]+')",  # an attribute `compile` of a name
        "import pkgutil\nNAMES = [m.name for m in pkgutil.iter_modules()]",
        "import math\nfloor = getattr(math, 'floor')",  # getattr with a literal that is no loader
        "def patch_up(values):\n    return values",  # `patch` is exactly the name; this is not
        "resolved_name = 'x'",
        "NOTE = 'run_module and patch are not looked up in a sentence'",
        "import runpy_not_really",
        "from unittest import TestCase",
    ],
)
def test_names_that_only_look_alike_are_not_reported(tmp_path: Path, line: str) -> None:
    assert found_in(tmp_path, line) == [], line


# --- aliases are taken from the whole file, in no particular order -----------------------------
# `ast.walk` goes by depth: a use can be visited before an import that stands above it in the
# source but nests deeper, and before one that follows it.

ALIAS_ORDER = [
    # the use precedes the import in the source and is shallower
    (
        "def early():\n    return ownvitals.db.engine\n\n\n"
        "def late():\n    if True:\n        class Box:\n            def method(self):\n"
        "                import ownvitals",
        "attribute access ownvitals.db",
    ),
    # the import precedes the use in the source and is deeper
    (
        "def setup():\n    if True:\n        with open_it():\n            import ownvitals\n\n\n"
        "ownvitals.db",
        "attribute access ownvitals.db",
    ),
    # a name bound by two imports means both modules, not the last one
    ("import ownvitals as x\nimport os as x\nx.db", "attribute access x.db"),
    ("import os as x\nimport ownvitals as x\nx.db", "attribute access x.db"),
    # the same for `builtins`, which the exec/eval/compile rule needs to know
    ("import builtins as b\nimport math as b\nb.exec('import httpx')", "use of builtins.exec"),
]


@pytest.mark.parametrize(("line", "fragment"), ALIAS_ORDER)
def test_an_alias_is_known_wherever_its_import_stands(
    tmp_path: Path, line: str, fragment: str
) -> None:
    found = found_in(tmp_path, line)
    assert any(fragment in v for v in found), (fragment, found)


# --- modules added to FORBIDDEN ----------------------------------------------------------------
# Listed by name: reading the names from FORBIDDEN would pass when an entry is removed.

ADDED = [
    "sqlalchemy",
    "psycopg",
    "requests",
    "ownvitals.context",
    "ownvitals.auth",
    "ownvitals.connections",
    # packages with I/O or web routes on main when #76 was written
    "ownvitals.admin",
    "ownvitals.flags",
    "ownvitals.cli",
    "ownvitals.config",
    "ownvitals.log",
    "ownvitals.headers",
]


@pytest.mark.parametrize("name", ADDED)
def test_each_added_module_fails_the_guard_in_every_import_form(tmp_path: Path, name: str) -> None:
    parent, _, leaf = name.rpartition(".")
    forms = [
        f"import {name}",
        f"import {name}.sub",
        f"import {name} as m",
        f"from {name} import thing",
        f"from {name}.sub import thing",
        *([f"from {parent} import {leaf}"] if parent else []),
        f"import importlib\nimportlib.import_module('{name}')",
    ]
    found = found_in(tmp_path, "\n".join(forms))
    lines = {v.split(":")[1] for v in found}
    # `import importlib` is a line of its own and reports nothing; every other form reports once
    assert len(lines) == len(forms), (name, found)


@pytest.mark.parametrize(
    "line",
    [
        "import requests_not_really",
        "import sqlalchemy_not_really",
        "from ownvitals.authors import thing",
        "from ownvitals.contextual import thing",
        "from ownvitals.configs import thing",
        "from ownvitals.logistics import thing",
        "from ownvitals.headers_not_really import thing",
        "from ownvitals.emailnorm import normalize_email",
    ],
)
def test_a_module_that_only_starts_like_a_forbidden_one_is_allowed(
    tmp_path: Path, line: str
) -> None:
    assert found_in(tmp_path, line) == [], line
