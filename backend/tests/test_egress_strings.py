"""The fourth FAIL on #123 (E1, E2): a string ``src`` writes may not name an importer, evaluator or
reflection word, nor parse as code that runs (a call, a lambda, a comprehension, a walrus, an
f-string); source text is never read back (``__doc__``, ``inspect``, ``pydoc``, ``linecache``,
``tokenize``). ``CODE_STRINGS`` lists the reviewed exceptions (``egress_importers``).
"""

import ast
from pathlib import Path

import pytest

from egress_classified import CODE_STRINGS
from egress_scan import scan_source

M = "ownvitals.web.planted"
SRC = Path(__file__).resolve().parent.parent / "src"
WORD = "an importer or evaluator, by string"
CODE = "parses as code"
SOURCE = "hands back source text"
DOC = "reads a docstring back"
RUNS = "a string annotation holds more than names"

# The reviewer's routes: each string reaches a type position as a value.
ROUTES = [
    "from pydantic import BaseModel\nT = \"__import__('cty' + 'pes') and int\"\n"
    "class M(BaseModel):\n    x: T = None",
    "U = \"__import__('_posix' + 'subprocess').fork_exec\"\ndef f(b: U) -> None: ...",
    "from typing import TypedDict\nTD = TypedDict('TD', {'x': \"__import__('cty' + 'pes').CDLL\"})",
    "from typing import NamedTuple\nNT = NamedTuple('NT', [('x', \"__import__('cty' + 'pes')\")])",
    "from pydantic import TypeAdapter\nTypeAdapter(\"__import__('cty' + 'pes')\")",
    "from typing import NewType\nN = NewType('N', \"__import__('json')\")",
    "type A = list[\"__import__('json')\"]",
    "import dataclasses\ndataclasses.make_dataclass('D', [('x', \"__import__('json')\")])",
    "def f(): ...\nf.__annotations__ = {'x': \"__import__('json')\"}",
    "from pydantic import BaseModel\nclass M(BaseModel):\n"
    "    x: (\"__import__('cty' + 'pes') and int\" if 1 else int) = None",
    "import sys\nT = \"sys.modules['_win' + 'api'] and int\"",
    "import builtins\nvars(builtins)['exec']('import cty' + 'pes')",
]
# E1a: a code word as an identifier segment, whatever else the string holds.
WORDS = [
    "s = '__import__'",
    "s = 'import_module'",
    "s = 'exec'",
    "s = 'eval(x)'",
    "s = 'compile'",
    "s = '__builtins__'",
    "s = 'builtins.open'",
    "s = 'sys.modules'",
    "s = 'getattr'",
    "s = 'vars'",
    "s = 'globals()'",
    "s = b'exec'",
]
# E1b: a string that parses as code that runs, with no code word.
CODE_RUNS = [
    "s = \"open('x').read()\"",
    "s = 'g()'",
    "s = '[x for x in y]'",
    "s = '{x: 1 for x in y}'",
    "s = '(x for x in y)'",
    "s = 'lambda: 1'",
    "s = '(y := 1)'",
    "s = \"f'{x}'\"",
    "s = \"list['g()']\"",  # a nested string runs too
    "s = 'MailMessage(...)'",  # reviewed only in ``ownvitals.mail``
]
# E2: reading source text back.
SOURCE_READ = [
    ('"""__import__(\'ctypes\') and int"""\nT = __doc__', DOC),
    ('def _d():\n    """x"""\nT = _d.__doc__', DOC),
    ("from typing import ForwardRef\ndef _d(): ...\nx = ForwardRef(_d.__doc__)", DOC),
    ("import inspect\ndef _d(): ...\nT = inspect.getcomments(_d)", SOURCE),
    ("from inspect import getdoc", SOURCE),
    ("import pydoc", SOURCE),
    ("import linecache\nlinecache.getline('x.py', 1)", SOURCE),
    ("import tokenize", SOURCE),
]
NEAR_MISSES = [
    "q = 'SELECT id FROM jobs WHERE owner_id = :owner AND state = ANY(:states)'",
    "msg = 'job %s finished in %.1f s'",
    "t = 'Node'",
    "h = 'X-Content-Type-Options'",  # a header name
    "m = 'text/event-stream'",  # a MIME type
    "v = 'daily/1'",  # an algo version id
    "e = 'smtp_host is not configured'",  # a comparison: reads names only
    "c = 'utf-8'\nd = 'no-store'\nr = 'postgresql+psycopg'",
    'j = \'{"level": "ERROR", "message": "x"}\'',  # a dict of constants
    'def f() -> None:\n    """Do not eval anything, nor read vars."""\n',  # a docstring
    "g = 'globally'\nb = 'evaluation'\nmods = 'module'",  # look-alike words
]
# M6, M7 of the fourth FAIL: only ``typing``'s ``Literal`` and ``Annotated`` are skipped.
LOCAL_GENERICS = [
    "class Literal:\n    pass\nx: Literal[\"__import__('json')\"]",
    "class Annotated:\n    pass\nx: Annotated[int, \"__import__('json')\"]",
]


@pytest.mark.parametrize("source", ROUTES)
def test_the_reviewers_routes_are_refused(source: str) -> None:
    found = [why for _, why in scan_source(source, M)]
    assert any(WORD in why or CODE in why or RUNS in why for why in found), found


@pytest.mark.parametrize("source", WORDS)
def test_a_code_word_in_a_string_is_refused(source: str) -> None:
    assert any(WORD in why for _, why in scan_source(source, M))


@pytest.mark.parametrize("source", CODE_RUNS)
def test_a_string_that_parses_as_running_code_is_refused(source: str) -> None:
    assert any(CODE in why for _, why in scan_source(source, M))


@pytest.mark.parametrize(("source", "why"), SOURCE_READ)
def test_source_text_is_never_read_back(source: str, why: str) -> None:
    found = [w for _, w in scan_source(source, M)]
    assert any(why in w for w in found), found


@pytest.mark.parametrize("source", NEAR_MISSES)
def test_the_near_misses_pass(source: str) -> None:
    assert scan_source(source, M) == []


@pytest.mark.parametrize("source", LOCAL_GENERICS)
def test_a_local_literal_or_annotated_is_read(source: str) -> None:
    assert any(RUNS in why for _, why in scan_source(source, M))


@pytest.mark.parametrize(("module", "text"), sorted(CODE_STRINGS))
def test_each_reviewed_string_is_in_src_and_passes_only_there(module: str, text: str) -> None:
    path = SRC / Path(*module.split("."))
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.Constant) and n.value == text for n in ast.walk(tree)), text
    source = f"s = {text!r}"
    assert scan_source(source, module) == []
    assert scan_source(source, M) != []
