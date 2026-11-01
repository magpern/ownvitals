"""The FAIL on #123 (C1, C2): module loaders and names in strings, refused in ``src`` and fail
closed (``egress_importers``). Stored and aliased ``import_module``/``__import__`` are in
``test_egress_reflection.py``.
"""

import pytest

from egress_scan import scan_source

MODULES = ["ownvitals.web.planted", "ownvitals.worker.loop", "ownvitals.mail", "ownvitals.cli"]
LOADS = "loads a module by a name the guard cannot judge"
BY_STRING = "by string"
BUILTINS = "reaches every importer by name"

# C1: a module loader, or an ``importlib`` name outside ``import_module`` and the metadata.
LOADERS = [
    "import runpy\nrunpy.run_module('ctypes.wintypes')['ctypes'].CDLL",
    "from runpy import run_module as r\nr('ctypes.wintypes')",
    "import runpy\nrunpy.run_path('/x.py')",
    "import pkgutil\npkgutil.get_loader('ctypes').load_module('ctypes')",
    "import pkgutil\npkgutil.find_loader('_posixsubprocess')",
    "import pkgutil\npkgutil.iter_modules()",
    "from importlib.machinery import PathFinder\ns = PathFinder.find_spec('ctypes')",
    "from importlib import machinery\nmachinery.BuiltinImporter.find_spec('_posixsubprocess')",
    "import importlib\nimportlib.machinery.ExtensionFileLoader",
    "import inspect\ninspect.importlib.machinery.PathFinder",  # a chain through a binding
    "from importlib._bootstrap import _gcd_import\n_gcd_import('ctypes')",
    "import importlib._bootstrap_external",
    "import _frozen_importlib\n_frozen_importlib._gcd_import('ctypes')",
    "import _imp",
    "import zipimport\nzipimport.zipimporter('x.zip')",
    "import importlib.abc\nimportlib.abc.Loader",
    "import importlib\nimportlib.reload(m)",
    "from importlib.util import LazyLoader",
    "from importlib.util import module_from_spec",
    "import importlib\nimportlib.invalidate_caches()",
]
# C2: a string naming a native or process module or an importer, and ``__builtins__``.
NAMED = [
    ("__builtins__['__import__']('ctypes')", BUILTINS),
    ("f = __builtins__.__import__\nf('ctypes')", BUILTINS),
    ("import builtins\nvars(builtins)['__import__']('_posixsubprocess').fork_exec", BY_STRING),
    ("import builtins\nbuiltins.__dict__['__import__']('ctypes')", BY_STRING),
    ("import importlib\nimportlib.__dict__['import_module']('ctypes')", BY_STRING),
    ("def f():\n    return f.__globals__['__builtins__']", BY_STRING),
    ("import os\nos.__builtins__", BUILTINS),
    ("name = 'ctypes'", BY_STRING),
    ("name = 'ctypes.util'", BY_STRING),
    ("name = '_ctypes'", BY_STRING),
    ("name = f'_posixsubprocess'", BY_STRING),
    ("name = '_winapi'", BY_STRING),
    ("name = 'multiprocessing.pool'", BY_STRING),
    ("name = 'concurrent.futures.process'", BY_STRING),
    ("name = 'import_module'", BY_STRING),
    ("import builtins\ndef f(x: vars(builtins)['__import__']('ctypes')) -> None: ...", BY_STRING),
]
NEAR_MISSES = [
    "from importlib.metadata import version\nversion('ownvitals')",
    "import importlib.metadata\nimportlib.metadata.version('ownvitals')",
    "from importlib import metadata\nmetadata.PackageNotFoundError",
    "from importlib.metadata import PackageNotFoundError, version",  # ``ownvitals/__init__.py``
    "import importlib\nimportlib.import_module('json')",
    "name = 'ctypesish'\nother = 'json'\nthird = 'multiprocessingx'",
    "name = 'concurrent.futures'\nother = 'process'\ntext = 'import json'",
    "raw = b'multiprocessingx'\nmore = b'json'",
    # D2 near misses: ``re.compile`` is not the builtin. (Docstrings naming ``ctypes`` are
    # refused since the fifth FAIL: ``test_egress_prose.py``.)
    "import re\nre.compile('x')",
    '"""This module mentions nothing refused."""\n',
    # Forward references that hold only names (``typing.get_type_hints`` evaluates them).
    "class Node:\n    nxt: 'Node'\n",
    "def f(x: 'list[Node]') -> 'Node': ...",
    "from collections.abc import Mapping\nx: 'Mapping[str, int] | None' = None",
    "from collections.abc import Callable\nf: 'Callable[[int], str]'",
    "from typing import Literal\nmode: \"Literal['a', 'b']\"",
    "x: \"tuple['Node', ...]\"",
    "builtins_ = 1\nimporter = 'import_module_x'",
]
# A string annotation is evaluated by ``typing.get_type_hints`` (FastAPI, pydantic): it may hold
# only names, judged as a name in code (as bound at run time, else as spelled).
RUNS = "a string annotation holds more than names"
FORWARD = [
    ("def f(x: \"__import__('ctypes').CDLL\") -> None: ...", RUNS),
    ("x: \"__import__('json')\" = None", RUNS),
    ("def f() -> 'run(1)': ...", RUNS),
    ("x: 'lambda: 1'", RUNS),
    ("x: '[n for n in y]'", RUNS),
    ("x: 'a if b else c'", RUNS),
    ("x: 'a + b'", RUNS),
    ("x: 'not valid python ('", RUNS),
    ("x: \"list['__import__(1)']\"", RUNS),  # a nested string is a forward reference too
    ("x: 'ctypes.c_int'", NATIVE := "calls native code the egress hook cannot see"),
    ("def f(x: 'list[_ctypes.CFuncPtr]') -> None: ...", NATIVE),
    (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import ctypes\n"
        "def f(x: 'ctypes.c_int') -> None: ...",
        NATIVE,
    ),
    ("x: 'sys.modules'", "resolves a module by name at run time"),
    ("x: 'runpy.run_module'", LOADS),
    ("x: 'socket.socket'", "can reach the network"),
]
# R2 of the FAIL on #123: an unresolved ``import_module`` callee is still judged.
# D2 of the FAIL on 95ee6f6: evaluators, by any alias or ``from`` form, and strings that name a
# refused module as an identifier segment (``str`` or ``bytes``).
EVALUATES = "evaluates code or data the guard cannot read"
EVALUATORS = [
    "exec('import ctypes')",
    "eval(\"__import__('json')\")",
    "compile('x', 'f', 'exec')",
    "f = exec",
    "import builtins\nbuiltins.exec('x')",
    "from builtins import eval as e\ne('1')",
    "import pickle\npickle.loads(b'cctypes\\nCDLL\\n(tR.')",
    "from pickle import loads",
    "import _pickle",
    "import marshal\nmarshal.loads(b'')",
    "import shelve",
    "import timeit\ntimeit.timeit('import ctypes', number=1)",
    "import code\ncode.InteractiveInterpreter().runsource('x')",
    "import codeop\ncodeop.compile_command('x')",
    "from pydantic import ImportString, TypeAdapter\n"
    "TypeAdapter(ImportString).validate_python('ctypes:CDLL')",
    "import pydantic\nclass M(pydantic.BaseModel):\n    f: pydantic.ImportString = 'x'",
    "from pydantic.types import ImportString",
]
STRINGS = [
    "name = 'ctypes:CDLL'",
    "path = 'ctypes/__init__.py'",
    "raw = b'ctypes'",
    "raw = 'import ctypes'.encode()",
    "raw = b'import _posixsubprocess'",
    "path = 'x.ctypes'",
    "path = 'concurrent/futures/process.py'",
    "text = f'{x} _winapi'",
    "values = ['json', 'multiprocessing']",
]
UNRESOLVED = [
    ("def f(lib):\n    return lib.import_module('ctypes')", "imports ctypes"),
    ("def f(lib, n):\n    return lib.import_module(n)", "imports dynamically"),
    ("def f(lib):\n    return lib.__import__('_posixsubprocess')", "imports _posixsubprocess"),
]


@pytest.mark.parametrize("source", LOADERS)
def test_a_module_loader_is_refused(source: str) -> None:
    for module in MODULES:
        found = scan_source(source, module)
        assert any(LOADS in why for _, why in found), (module, found)


@pytest.mark.parametrize(("source", "why"), NAMED)
def test_a_name_in_a_string_or_builtins_is_refused(source: str, why: str) -> None:
    for module in MODULES:
        found = scan_source(source, module)
        assert any(why in w for _, w in found), (module, found)


@pytest.mark.parametrize("source", NEAR_MISSES)
def test_the_near_misses_pass(source: str) -> None:
    assert scan_source(source, "ownvitals.web.planted") == []


@pytest.mark.parametrize(("source", "why"), FORWARD)
def test_a_string_annotation_holds_only_names_judged_as_code(source: str, why: str) -> None:
    found = scan_source(source, "ownvitals.web.planted")
    assert any(why in w for _, w in found), found


@pytest.mark.parametrize("source", EVALUATORS)
def test_an_evaluator_is_refused(source: str) -> None:
    for module in MODULES:
        found = scan_source(source, module)
        assert any(EVALUATES in why for _, why in found), (module, found)


@pytest.mark.parametrize("source", STRINGS)
def test_a_string_naming_a_refused_module_is_refused(source: str) -> None:
    found = scan_source(source, "ownvitals.web.planted")
    assert any(BY_STRING in why for _, why in found), found


def test_a_module_attr_string_is_judged_as_its_dotted_name() -> None:
    found = scan_source("target = 'socket:create_connection'", "ownvitals.web.planted")
    assert any("can reach the network" in why for _, why in found), found


@pytest.mark.parametrize(("source", "why"), UNRESOLVED)
def test_an_unresolved_importer_call_is_judged(source: str, why: str) -> None:
    found = scan_source(source, "ownvitals.web.planted")
    assert any(why in w for _, w in found), found
