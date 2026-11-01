"""Issue #122 (follow-ups NB1 and NB2 of the Opus PASS on #55): the static guard refuses a module
lookup through ``sys.modules`` and the use of ``ctypes`` in ``src/``.

``sys.modules["_posixsubprocess"]`` hands out a process route every name-reading rule misses
(``subprocess`` imports it, so it is always loaded); ``ctypes`` reaches the C library's own
process and socket functions, which raise no audit event the hook sees (ADR 0009 item 7). Both
are refused under any alias, ``from`` form or chain through a module's import bindings.
"""

from pathlib import Path

import pytest

from egress_classified import NATIVE_MODULES, REFLECTION_NAMES
from egress_scan import scan_source, scan_tree

SRC = Path(__file__).resolve().parent.parent / "src"
LOOKUP = "resolves a module by name at run time"
NATIVE = "calls native code the egress hook cannot see"
MODULES = ["ownvitals.web.planted", "ownvitals.worker.loop", "ownvitals.mail", "ownvitals.cli"]

SYS_MODULES = [
    "import sys\nsys.modules['_posixsubprocess'].fork_exec()",
    "import sys\nsys.modules['multiprocessing']",
    "import sys\nsys.modules.get('_posixsubprocess')",
    "import sys\nsys.modules.get('x', None)",
    "import sys as s\ns.modules['_posixsubprocess']",
    "import sys as s\ns.modules.get('_winapi')",
    "from sys import modules\nmodules['_posixsubprocess']",
    "from sys import modules as m\nm.get('_posixsubprocess')",
    "import sys\nloaded = sys.modules\nloaded['_posixsubprocess']",
    "import sys\ngetattr(sys, 'modules')['_posixsubprocess']",
    "import sys\n'_posixsubprocess' in sys.modules",
    "def f():\n    import sys as s\n    return s.modules['_winapi']",
    "import os\nos.sys.modules['_posixsubprocess']",  # a chain through ``os``'s binding of sys
    "import pkgutil\npkgutil.resolve_name('_posixsubprocess:fork_exec')",  # the earlier rule
]
CTYPES = [
    "import ctypes\nctypes.CDLL(None).system(b'x')",
    "import ctypes",
    "import ctypes as c\nc.CDLL(None)",
    "import ctypes.util",
    "import ctypes.util as u\nu.find_library('c')",
    "from ctypes import CDLL\nCDLL(None)",
    "from ctypes import CDLL as L\nL(None)",
    "from ctypes import util",
    "from ctypes.util import find_library",
    "import _ctypes",
    "import _ctypes as c\nc.dlopen",
    "from _ctypes import dlopen",
    "import importlib\nimportlib.import_module('ctypes')",
    "__import__('_ctypes')",
    "def f():\n    import ctypes\n    return ctypes.pythonapi",
    "try:\n    import ctypes\nexcept ImportError:\n    pass",
    "import typing\nif not typing.TYPE_CHECKING:\n    import ctypes",
]
NEAR_MISSES = [
    "import sys\nsys.argv",
    "import sys\nsys.exit(0)",
    "import sys\nsys.stdout.write('x')",
    "import sys\nsys.version_info >= (3, 12)",
    "modules = {}\nmodules['x']",  # a local, not sys's
    "class A:\n    modules: dict[str, int] = {}\n\nA.modules.get('x')",
    "import types\ntypes.ModuleType",
    "import importlib\nimportlib.import_module('json')",
    "text = 'sys.modules'\nname = 'ctypes'",  # strings name no capable module
    "ctypes_count = 1\nsys_modules = {}\nsys_modules['x']",  # names that only look alike
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import ctypes\n"
    "def f(x: 'ctypes.c_int') -> None: ...",  # type-only: never runs (docs/dev/egress.md)
]


@pytest.mark.parametrize("source", SYS_MODULES)
def test_the_guard_refuses_a_sys_modules_lookup(source: str) -> None:
    for module in MODULES:
        found = scan_source(source, module)
        assert any(LOOKUP in why for _, why in found), (module, found)


@pytest.mark.parametrize("source", CTYPES)
def test_the_guard_refuses_ctypes(source: str) -> None:
    for module in MODULES:
        found = scan_source(source, module)
        assert any(NATIVE in why for _, why in found), (module, found)


@pytest.mark.parametrize("source", NEAR_MISSES)
def test_the_guard_allows_the_near_misses(source: str) -> None:
    assert scan_source(source, "ownvitals.web.planted") == []


def test_the_rules_are_these() -> None:
    assert set(NATIVE_MODULES) == {"ctypes", "_ctypes"}
    assert set(REFLECTION_NAMES) == {"sys.modules", "pkgutil.resolve_name"}


def test_src_has_no_lookup_by_name_and_no_ctypes() -> None:
    assert [f for f in scan_tree(SRC) if LOOKUP in f[2] or NATIVE in f[2]] == []
