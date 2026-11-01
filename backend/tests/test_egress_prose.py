"""The fifth FAIL on #123: docstrings and comments are judged like any other string (F2, F3),
``PROCESS_NAMES`` join the string rule (F1), and more evaluators are refused (F4). Each reader
route below is refused because the docstring or comment it reads is refused.
"""

import pytest

from egress_scan import scan_source

M = "ownvitals.web.planted"
NATIVE = "a native or process module, by string"
WORD = "an importer or evaluator, by string"
SPANS = "prose that spans lines"
EVALUATES = "evaluates code or data the guard cannot read"
SOURCE = "hands back source text"
DOC = "'''__import__('ctypes') and int'''"  # a docstring that names an importer whole

# F2: docstrings read back without the name ``__doc__`` in code, refused by the docstring.
DOCSTRING_ROUTES = [
    f"def _d():\n    {DOC}\nT = getattr(_d, '__doc__')",
    f"class C:\n    {DOC}\nT = vars(C)['__doc__']",
    f"class C:\n    {DOC}\nT = C.__dict__['__doc__']",
    f"{DOC}\nT = globals()['__doc__']",
    f"def _d():\n    {DOC}\nT = _d.__code__.co_consts[0]",
]
# F3: a comment read back through ``traceback`` (and its ``linecache``), refused twice over.
COMMENT_ROUTE = (
    "import traceback\n"
    "T = traceback.extract_stack()[-1].line.rpartition('# ')[2]  # __import__('ctypes') and int"
)
# Prose: a native or process module, or an evaluator or importer as a whole identifier
# (``PROSE_WORDS``, the seventh FAIL on #123), is refused; the other code words are not applied.
PROSE_IMPORTER = "an evaluator or importer, in prose"
PROSE = [
    ('"""See ctypes for details."""', NATIVE),
    ('"""First line.\n\n    Uses multiprocessing.pool here."""', NATIVE),
    ("x = 1  # uses ctypes", NATIVE),
    ("x = 1  # __import__('json')", PROSE_IMPORTER),
    ("x = 1  # see import_module", PROSE_IMPORTER),
    ('def f():\n    """Reads __builtins__."""', PROSE_IMPORTER),
    ('"""concurrent.futures.\nprocess"""', SPANS),  # a module name split over lines
    # The seventh FAIL on #123: evaluator words, whatever the line parses to.
    ("x = 1  # docker compose exec web", PROSE_IMPORTER),
    ("x = 1  # no eval script", PROSE_IMPORTER),
    ("x = 1  # compile the regex once", PROSE_IMPORTER),
    ('def f():\n    """Calls eval once, on F(t)."""', PROSE_IMPORTER),
]
PROSE_NEAR_MISSES = [
    "x = 1  # the analysis modules",
    "x = 1  # env vars never set secrets",
    "x = 1  # getattr is not used",
    "x = 1  # no globals here",
    "x = 1  # builtins only",
    "x = 1  # max(MAD_SCALE * mad, min_scale)",  # a formula: no refused call
    "x = 1  # the executor and the evaluation",  # not whole identifiers
    "x = 1  # the source code, a trace, a profile",  # common English, not words (PROSE_WORDS)
]
# F1: the process names as strings (``concurrent.futures`` imports ``process`` lazily).
PROCESS_NAMES = [
    "T = 'concurrent.futures.ProcessPoolExecutor'",
    "T = 'futures.ProcessPoolExecutor'",
    "import concurrent.futures\nfrom pydantic import BaseModel, ConfigDict\n"
    "T = 'concurrent.futures.ProcessPoolExecutor'\nclass M(BaseModel):\n"
    "    model_config = ConfigDict(arbitrary_types_allowed=True)\n    x: T = None",
    "from pydantic import TypeAdapter\nTypeAdapter('concurrent.futures.ProcessPoolExecutor')",
    "x = 1  # ProcessPoolExecutor",
]
# F4: more evaluators and readers, under any alias or ``from`` form.
EVALUATORS = [
    ("import cProfile\ncProfile.run('x')", EVALUATES),
    ("from cProfile import run as r", EVALUATES),
    ("import profile", EVALUATES),
    ("import trace\ntrace.Trace().run('x')", EVALUATES),
    ("import doctest\ndoctest.run_docstring_examples(f, {})", EVALUATES),
    ("import pdb as p", EVALUATES),
    ("from bdb import Bdb", EVALUATES),
    ("import traceback", SOURCE),
    ("from traceback import extract_stack", SOURCE),
]
NEAR_MISSES = [
    '"""Load the settings and check them."""',
    'def f() -> None:\n    """Return the version, e.g. 1.2."""',
    "x = 1  # the scale is a robust estimate",
    "x = 1  # see docs/dev/egress.md",
    "x = 1  # noqa: E501",
    "x = {'modules': 1}",  # not the ECG module: still refused elsewhere (below)
]


@pytest.mark.parametrize("source", DOCSTRING_ROUTES)
def test_a_docstring_read_back_is_refused_by_its_text(source: str) -> None:
    assert any(NATIVE in why or WORD in why for _, why in scan_source(source, M))


def test_a_comment_read_back_is_refused_by_its_text() -> None:
    found = [why for _, why in scan_source(COMMENT_ROUTE, M)]
    assert any(NATIVE in why or WORD in why for why in found), found
    assert any(SOURCE in why for why in found), found


@pytest.mark.parametrize(("source", "why"), PROSE)
def test_docstrings_and_comments_are_judged(source: str, why: str) -> None:
    found = [w for _, w in scan_source(source, M)]
    assert any(why in w for w in found), found


@pytest.mark.parametrize("source", PROCESS_NAMES)
def test_a_process_name_in_a_string_is_refused(source: str) -> None:
    found = [why for _, why in scan_source(source, M)]
    assert any("ProcessPoolExecutor" in why and NATIVE in why for why in found), found


@pytest.mark.parametrize(("source", "why"), EVALUATORS)
def test_the_further_evaluators_and_readers_are_refused(source: str, why: str) -> None:
    found = [w for _, w in scan_source(source, M)]
    assert any(why in w for w in found), found


@pytest.mark.parametrize("source", NEAR_MISSES[:-1] + PROSE_NEAR_MISSES)
def test_the_near_misses_pass(source: str) -> None:
    assert scan_source(source, M) == []


def test_the_modules_key_passes_only_as_the_ecg_records_dict_key() -> None:
    pipeline = "ownvitals.ecg.pipeline"
    assert scan_source("record = {'modules': 1}", pipeline) == []
    for source in ("import sys\nsys.__dict__['modules']", "x = 'modules'", "f(key='modules')"):
        assert any(WORD in why for _, why in scan_source(source, pipeline)), source
    assert any(WORD in why for _, why in scan_source(NEAR_MISSES[-1], M))
