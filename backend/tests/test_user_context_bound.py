"""Guard 5's attribute rule, continued (#89, the Architect's fixes for the eleventh #115 review):
the audit event's comparison is pinned to ``_audit``'s own parameter; comments are judged for the
token's names and the context module, docstrings for dunders; ``linecache``, ``tokenize`` and
``importlib.util`` are refused; the context module is refused as an attribute of its package.

The review's forgeries run here to an admin context where that needs no file of its own, and
the scan must refuse each one's source.
"""

import importlib
import linecache
import string
from pathlib import Path
from typing import Any

import pytest

from ctx import make_context
from ownvitals.context import require_context
from test_user_context_writers import body
from user_context_scan import scan_source

HOOK = "ownvitals/egress/hook.py"
LOG = "ownvitals/log.py"
QUEUE = "ownvitals/db/queue_check.py"
AUDIT = "def _audit(event, args):\n    if event == 'socket.__new__':\n        return args\n"


def _admin(ctx: Any, tok: object) -> Any:
    kw = {"issuer": tok}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


def _token(namespace: dict[str, Any]) -> object:
    return next(v for v in namespace.values() if type(v) is object)


def _line_of(text: str) -> int:
    lines = Path(__file__).read_text("utf-8").splitlines()
    return lines.index(text) + 1


# B2: a comment read back with ``linecache``.
# ownvitals.context:_ISSUER
#: 0.__globals__
def comment_token(ctx: Any) -> Any:
    import pkgutil

    line = linecache.getline(__file__, _line_of("# ownvitals.context:_ISSUER"))
    return _admin(ctx, pkgutil.resolve_name(line[2:].strip()))


def comment_dunder(ctx: Any) -> Any:
    line = linecache.getline(__file__, _line_of("#: 0.__globals__"))
    g, _ = string.Formatter().get_field(line[3:].strip(), (require_context,), {})
    return _admin(ctx, _token(g))


# B3: the context module as an attribute of its package.
def package_attribute(ctx: Any) -> Any:
    m = importlib.import_module("ownvitals").context
    return _admin(ctx, _token(vars(m)))


FORGERIES = {f.__name__: f for f in (comment_token, comment_dunder, package_attribute)}


@pytest.mark.parametrize("forgery", FORGERIES.values(), ids=FORGERIES.keys())
def test_each_forgery_runs_to_an_admin_context(forgery: Any) -> None:
    assert forgery(make_context(role="user")).role == "admin"


RECORDER = """\
seen = []
class Rec:
    def __eq__(self, other):
        seen.append(other)
        return False
def install(ctx):
    event = Rec()
    if event == 'socket.__new__':
        pass
    n = seen[-1].partition(".")[2]
    setattr(type(ctx), n, seen)
"""
MOCK = """\
from unittest.mock import MagicMock
def install(ctx):
    event = MagicMock()
    event == 'socket.__new__'
    return event.mock_calls[0].args[0]
"""
D1 = """\
import linecache, pkgutil
# ownvitals.context:_ISSUER
def escalate(ctx, path):
    return pkgutil.resolve_name(linecache.getline(path, 2)[2:].strip())
"""
D2 = """\
import linecache, string
#: 0.__globals__
def escalate(ctx, path):
    return string.Formatter().get_field(linecache.getline(path, 2)[3:].strip(), (f,), {})
"""
D3 = """\
import ast, importlib.util
def h():
    "0.__globals__"
def escalate(ctx):
    origin = importlib.util.find_spec("ownvitals.web").origin
    return ast.get_docstring(ast.parse(open(origin).read()).body[1])
"""
REFUSED = {
    # B1: the event name compared anywhere but on ``_audit``'s own parameter, or twice.
    "a recording __eq__": RECORDER,
    "a mock": MOCK,
    "the parameter rebound": AUDIT.replace("    if", "    event = Rec()\n    if"),
    "the comparison in another function": AUDIT + AUDIT.replace("_audit", "_again"),
    "the comparison twice in _audit": AUDIT
    + "    if event == 'socket.__new__':\n        return args\n",
    "an except-as name": "try:\n    pass\nexcept E as __x__:\n    pass\n",
    "a subscript's context": body("return mods[0].context"),
    "the package by subscript": body("import sys", "return sys.modules['ownvitals']"),
    "a lambda on __name__": body("return (lambda __name__: __name__ == '__main__')(r)"),
    # B2: comments, docstrings, and the readers of source text.
    "D1, a comment with the token": D1,
    "D2, a comment with a dunder": D2,
    "D3, a docstring read from the file": D3,
    "a comment naming the module": "x = 1  # see ownvitals:context\n",
    "a comment naming the token": "# _ISSUER is private\n",
    "a docstring dunder": "def h():\n    '''Reads ``x.__globals__``.'''\n",
    "an allowed docstring dunder elsewhere": "def h():\n    '''Has ``__str__``.'''\n",
    "import linecache": "import linecache\n",
    "from tokenize": "from tokenize import generate_tokens\n",
    "import importlib.util as u": "import importlib.util as u\n",
    "from importlib import util": "from importlib import util\n",
    "from importlib.util import find_spec": "from importlib.util import find_spec\n",
    "another find_spec": "import importlib.util\nx = importlib.util.find_spec('ownvitals.web')\n",
    "the util attribute": "import importlib\nx = importlib.util\n",
    # B3: the context module from its package.
    "import_module('ownvitals').context": body(
        "import importlib", "return importlib.import_module('ownvitals').context"
    ),
    "resolve_name('ownvitals').context": body(
        "import pkgutil", "return pkgutil.resolve_name('ownvitals').context"
    ),
    "sys.modules['ownvitals'].context": body(
        "import sys", "return sys.modules['ownvitals'].context"
    ),
    "getattr(o, 'context')": body("import ownvitals as o", "return getattr(o, 'context')"),
    "a relative import of ..context": body(
        "import importlib", "return importlib.import_module('..context', 'ownvitals.db')"
    ),
    "vars(o)['context']": body("return vars(o)['context']"),
    "the package looked up": body("import sys", "return sys.modules.get('ownvitals')"),
    # The eleventh review's N2: Ma, Mb and Md.
    "a placeholder compared inside its assignment": "QUEUE_QUERY = x == '__NAMES__'\n",
    "a placeholder with a second target": "QUEUE_QUERY = alias = '(__NAMES__)'\n",
    "a dotted path after the module": body("return locate('ownvitals.context.UserContext')"),
}
NEAR_MISSES = {
    "the audit comparison": AUDIT,
    "the uvloop check": "import importlib.util\nx = importlib.util.find_spec('uvloop')\n",
    "an allowed docstring dunder in its module": "def h():\n    '''Has ``__str__``.'''\n",
    "a comment without a dunder": "x = 1  # no context here\n",
    "a comment naming another module": "x = 1  # see ownvitals.db\n",
    "a plain import of the module": "import ownvitals.context\n",
    "another object's context": body("return self.context.wrap_socket(s)"),
    "the package name as a value": body("return version('ownvitals')"),
    "the placeholders' assignment": "QUEUE_QUERY = '(__NAMES__)'.replace('__NAMES__', n)\n",
}
MODULES = {
    "the audit comparison": HOOK,
    "the uvloop check": HOOK,
    "an allowed docstring dunder in its module": LOG,
    "the placeholders' assignment": QUEUE,
}


@pytest.mark.parametrize(("case", "source"), REFUSED.items(), ids=REFUSED.keys())
def test_the_scan_refuses_each_spelling(case: str, source: str) -> None:
    for module in (HOOK, QUEUE, LOG, None):
        if case == "an allowed docstring dunder elsewhere" and module == LOG:
            continue  # its own module: a near miss below
        assert scan_source(source, module) != [], module


@pytest.mark.parametrize(("case", "source"), NEAR_MISSES.items(), ids=NEAR_MISSES.keys())
def test_each_near_miss_stays_allowed(case: str, source: str) -> None:
    assert scan_source(source, MODULES.get(case)) == []


def test_the_uvloop_check_is_allowed_only_in_hook_py() -> None:
    check = NEAR_MISSES["the uvloop check"]
    assert scan_source(check, HOOK) == []
    assert scan_source(check, QUEUE) != []


def test_importlib_util_is_refused_outside_its_one_use() -> None:
    assert scan_source("import importlib.util\n", QUEUE) != []  # the import alone
    call = "import importlib\nx = importlib.util.find_spec('uvloop')\n"
    assert scan_source(call, QUEUE) != []  # the call alone, without importing the submodule
