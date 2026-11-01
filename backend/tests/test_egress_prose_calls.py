"""The sixth FAIL on #123: ``logging``, ``warnings`` and ``tracemalloc`` hand source lines to an
evaluator (B1), so a docstring line or comment that parses as a call to an evaluator, importer or
reflection name is refused; ``help`` and ``breakpoint`` import by a computed name (B2).
``PROSE_LINES`` pass only as prose in their own module (Q3).
"""

import ast
import io
import tokenize
from pathlib import Path

import pytest

from egress_classified import PROSE_LINES
from egress_scan import scan_source

M = "ownvitals.web.planted"
SRC = Path(__file__).resolve().parent.parent / "src"
CALLS = "in prose: a reader could hand it to pydantic"
EVALUATES = "evaluates code or data the guard cannot read"
WORD = "an importer or evaluator, by string"
PAYLOAD = "# exec('import cty'+'pes', {}) or int"
MODEL = "\nfrom pydantic import BaseModel\nclass Mo(BaseModel):\n    x: T = None\n"

# B1: the reviewer's four reader routes, each carrying the payload in a comment.
READER_ROUTES = [
    "import logging\n"
    f"T = logging.getLogger().findCaller(stack_info=True)[3].rpartition('# ')[2]  {PAYLOAD}"
    + MODEL,
    "import logging\ntry:\n"
    f"    1 / 0  {PAYLOAD}\n"
    "except Exception as e:\n"
    "    T = logging.Formatter().formatException((type(e), e, e.__traceback__))" + MODEL,
    "import warnings\n"
    f"T = warnings.formatwarning('', Warning, __file__, 2).strip().rpartition('# ')[2]  {PAYLOAD}"
    + MODEL,
    "import tracemalloc\ntracemalloc.start()\n"
    f"obj = [1]  {PAYLOAD}\n"
    "T = tracemalloc.get_object_traceback(obj).format()[-1]" + MODEL,
    # A listed reader (N9: ``co_consts``) carrying the payload in a docstring.
    "def _d():\n    '''exec('import cty'+'pes', {}) or int'''\nT = _d.__code__.co_consts[0]"
    + MODEL,
]
PROSE_CALLS = [
    "x = 1  # help('cty'+'pes') or int",
    "x = 1  # eval('1')",
    "x = 1  # compile('x', 'f', 'exec')",
    "x = 1  # obj.getattr(y)",  # an attribute ending in one of the names
    "x = 1  # setattr(a, b, c)",
    "x = 1  # vars(sys)['x']",
    "x = 1  # globals()['x']",
    "x = 1  # locals()",
    "x = 1  # breakpoint()",
    "x = 1  # open('f').read()",
    "x = 1  # runpy.run_module(n)",
    "x = 1  # runpy.run_path(p)",
    'def f():\n    """Example.\n\n    >>> eval("1")\n    """',  # a doctest marker
    'def f():\n    """Steps:\n\n    - exec(code)\n    """',  # a list marker
    'def f():\n    """1. exec(code) or int"""',
]
PROSE_NEAR_MISSES = [
    "x = 1  # max(MAD_SCALE * mad, min_scale)",  # the formula comments in ``src``
    "x = 1  # F(t)",
    "x = 1  # S / (n(n-1)/2)",
    "x = 1  # exec is never called here",
    "x = 1  # see the open issues",
    'def f():\n    """Calls eval once, on F(t)."""',  # prose that does not parse
    "x = 1  # len(items) - 1",
]
# B2: ``help`` and ``breakpoint``.
HELP_BREAKPOINT = [
    ("help('cty' + 'pes')", EVALUATES),
    (
        "import os\nos.environ['PYTHONBREAKPOINT'] = 'cty' + 'pes.c_int'\nbreakpoint(5)",
        EVALUATES,
    ),
    ("x = 'PYTHONBREAKPOINT'", WORD),
    ("import builtins\nbuiltins.help", EVALUATES),
    ("h = help", EVALUATES),
    ("import sys\nsys.breakpointhook()", EVALUATES),
    ("from sys import breakpointhook", EVALUATES),
]
ARGPARSE = (
    "import argparse\np = argparse.ArgumentParser()\n"
    "p.add_argument('--host', help='the host to bind')"
)


@pytest.mark.parametrize("source", READER_ROUTES)
def test_a_reader_route_is_refused_by_its_payload(source: str) -> None:
    found = [why for _, why in scan_source(source, M)]
    assert any(f"calls exec {CALLS}" in why for why in found), found


@pytest.mark.parametrize("source", PROSE_CALLS)
def test_a_call_to_an_evaluator_in_prose_is_refused(source: str) -> None:
    found = [why for _, why in scan_source(source, M)]
    assert any(CALLS in why for why in found), found


@pytest.mark.parametrize("source", [*PROSE_NEAR_MISSES, ARGPARSE])
def test_the_near_misses_pass(source: str) -> None:
    assert scan_source(source, M) == []


@pytest.mark.parametrize(("source", "why"), HELP_BREAKPOINT)
def test_help_and_breakpoint_are_refused(source: str, why: str) -> None:
    found = [w for _, w in scan_source(source, M)]
    assert any(why in w for w in found), found


def _prose(module: str) -> set[str]:
    path = SRC / Path(*module.split("."))
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    source = path.read_text(encoding="utf-8")
    lines = {
        line.strip()
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
        if isinstance(n.value.value, str)
        for line in n.value.value.splitlines()
    }
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return lines | {t.string.lstrip("#").strip() for t in tokens if t.type == tokenize.COMMENT}


@pytest.mark.parametrize(("module", "line"), sorted(PROSE_LINES))
def test_each_reviewed_prose_line_is_in_src_and_passes_only_there(module: str, line: str) -> None:
    assert line in _prose(module), line  # a docstring line or a comment of that module
    docstring = f'"""{line}"""\n'
    assert scan_source(docstring, module) == []
    assert scan_source(docstring, M) != []  # Q3: not in another module
    assert scan_source(f"s = {line!r}", module) != []  # N4: not as a string code reads


def test_a_reviewed_string_never_excepts_a_module_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """N4: the module rule runs before ``CODE_STRINGS``, so an entry naming one excepts nothing."""
    import egress_importers

    planted = "ctypes.CDLL(None)"
    monkeypatch.setattr(egress_importers, "CODE_STRINGS", frozenset({(M, planted)}))
    found = [why for _, why in scan_source(f"s = {planted!r}", M)]
    assert any("a native or process module, by string" in why for why in found), found
