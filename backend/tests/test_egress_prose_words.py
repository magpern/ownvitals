"""The seventh FAIL on #123: the call rule in prose could be spelled around (B1), so a docstring or
comment holding an evaluator or importer name as a whole identifier (``PROSE_WORDS``) is refused,
whatever the line parses to; ``help`` and ``breakpoint`` had more spellings (B2).
"""

import pytest

from egress_classified import PROSE_LINES
from egress_scan import scan_source

M = "ownvitals.web.planted"
PROSE = "an evaluator or importer, in prose"
CALLS = "in prose: a reader could hand it to pydantic"
EVALUATES = "evaluates code or data the guard cannot read"
WORD = "an importer or evaluator, by string"
MODEL = "\nfrom pydantic import BaseModel\nclass Mo(BaseModel):\n    x: T = None\n"
READER = "logging.getLogger().findCaller(stack_info=True)[3]"
CALL = "exec('import cty'+'pes', {}) or int"

# B1: the reviewer's routes, each read back by a reader the guard allows.
B1_ROUTES = [
    f"import logging\nT = {READER}.rpartition('# ')[2]  # [exec][0]('import cty'+'pes', {{}})"
    + MODEL,
    f"import logging\nT = {READER}.rpartition('# ')[2]  # (exec if 1 else 0)('x', {{}}) or int"
    + MODEL,
    f"import logging\nT = {READER}.rpartition('# ')[2]  # \"{CALL}\"" + MODEL,
    f"import logging\nT = {READER}.rpartition(': ')[2]  #: {CALL}" + MODEL,
    f"import logging\nT = {READER}.rpartition(': ')[2]  # Note: {CALL}" + MODEL,
    "import logging\n"
    f"a = {READER}.rpartition('# ')[2]  # exec('import cty'\n"
    f"b = {READER}.rpartition('# ')[2]  # +'pes', {{}}) or int\n"
    "T = a.strip() + b.strip()" + MODEL,
    # A multi-line docstring read through ``co_consts`` (non-blocking item 2).
    "def _d():\n    '''exec('import cty'\n    +'pes', {}) or int'''\nT = _d.__code__.co_consts[0]"
    + MODEL,
    "x = 1  # \uff45\uff58\uff45\uff43('x')",  # fullwidth exec: NFKC, as Python reads names
    "x = 1  # TODO: eval(s)",
    "x = 1  #! compile(s, 'f', 'exec')",
    'def f():\n    """Uses help, breakpoint and sys.__breakpointhook__."""',
    "x = 1  # runpy, pkgutil.resolve_name, importlib, find_spec, load_module",
    "x = 1  # pickle, marshal, ImportString, _sitebuiltins._Helper",
]
# B2: ``help`` and ``breakpoint`` under the spellings the sixth round missed.
B2_ROUTES = [
    (
        "import os, sys\nos.environ['PYTHON' + 'BREAKPOINT'] = 'cty' + 'pes.c_int'\n"
        "sys.__breakpointhook__(5)",
        EVALUATES,
    ),
    ("import _sitebuiltins\n_sitebuiltins._Helper()('cty' + 'pes')", EVALUATES),
    ("from _sitebuiltins import _Helper", EVALUATES),
    ("import builtins\nvars(builtins)['help']('cty' + 'pes')", WORD),
    ("import builtins\nbuiltins.__dict__['help']('cty' + 'pes')", WORD),
    ("x = 'breakpoint'", WORD),
    ("x = b'breakpointhook'", WORD),
    ("x = '__breakpointhook__'", WORD),
]
ARGPARSE = (
    "import argparse\np = argparse.ArgumentParser()\nsub = p.add_subparsers(help='commands')\n"
    "p.add_argument('--host', help='the host to bind')"
)


@pytest.mark.parametrize("source", B1_ROUTES)
def test_an_evaluator_or_importer_word_in_prose_is_refused(source: str) -> None:
    found = [why for _, why in scan_source(source, M)]
    assert any(PROSE in why for why in found), found


@pytest.mark.parametrize(("source", "why"), B2_ROUTES)
def test_help_and_breakpoint_spellings_are_refused(source: str, why: str) -> None:
    found = [w for _, w in scan_source(source, M)]
    assert any(why in w for w in found), found


def test_argparse_help_keywords_pass() -> None:
    assert scan_source(ARGPARSE, M) == []


def test_a_double_hash_comment_is_judged_by_the_call_rule() -> None:
    """NB1 (mutation R4): ``##`` strips to the line, not to another comment."""
    found = [why for _, why in scan_source("x = 1  ## getattr(a, b)", M)]
    assert any(f"calls getattr {CALLS}" in why for why in found), found


@pytest.mark.parametrize(("module", "line"), sorted(PROSE_LINES))
def test_a_reviewed_line_passes_only_whole(module: str, line: str) -> None:
    """``PROSE_LINES`` match the full stripped line, never a prefix or a part of it."""
    assert scan_source(f'"""{line}"""\n', module) == []
    assert scan_source(f"x = 1  # {line}\n", module) == []
    assert scan_source(f'"""{line} or exec(s)"""\n', module) != []
    assert scan_source(f'"""eval(s) {line}"""\n', module) != []


def test_a_reviewed_line_does_not_hide_a_word_on_the_next_line() -> None:
    module, line = sorted(PROSE_LINES)[0]
    found = [why for _, why in scan_source(f'"""{line}\n    eval(s)"""\n', module)]
    assert any(PROSE in why for why in found), found
