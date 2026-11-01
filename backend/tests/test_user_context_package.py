"""Guard 5, continued (#89, the Architect's fixes for the twelfth #115 review): the package
``ownvitals`` only as the root of an attribute chain that doesn't continue with ``context``; no
string naming ``context``; no relative ``import_module``; and dunders in comments judged against
the table, since ``traceback`` and ``warnings`` read comments back.

The review's forgeries run here to an admin context, and the scan must refuse each one's source.
"""

import importlib
import inspect
import operator
import string
import sys
import textwrap
import traceback
import warnings
from typing import Any

import pytest

import ownvitals.db
from ctx import make_context
from ownvitals.context import require_context
from test_user_context_writers import body
from user_context_scan import scan_source

HOOK = "ownvitals/egress/hook.py"
# ``self.context.wrap_socket(...)`` in a method: allowed in ``mail.py`` only.
WRAP_SOCKET = "class Mailer:\n    def send(self, s):\n        return self.context.wrap_socket(s)\n"
CONFIG = "ownvitals/config.py"


def _admin(ctx: Any, tok: object) -> Any:
    kw = {"issuer": tok}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


def _token(namespace: dict[str, Any]) -> object:
    return next(v for v in namespace.values() if type(v) is object)


# B1: the context module from its package.
def p1_rebound(ctx: Any) -> Any:
    o = ownvitals
    return _admin(ctx, _token(vars(o.context)))  # pyright: ignore[reportAttributeAccessIssue]


def p2_relative_dot(ctx: Any) -> Any:
    p = importlib.import_module(".", "ownvitals")
    return _admin(ctx, _token(vars(p.context)))


def p3_relative_parent(ctx: Any) -> Any:
    p = importlib.import_module("..", "ownvitals.db")
    return _admin(ctx, _token(vars(p.context)))


def p4_attrgetter(ctx: Any) -> Any:
    m = operator.attrgetter("context")(importlib.import_module(".", "ownvitals"))
    return _admin(ctx, _token(vars(m)))


def p5_vars(ctx: Any) -> Any:
    m = next(v for k, v in vars(ownvitals).items() if k == "context")
    return _admin(ctx, _token(vars(m)))


# B2: a comment read back by ``traceback`` or ``warnings``.
def t1_traceback(ctx: Any) -> Any:
    line = traceback.extract_stack()[-1].line or ""  #: 0.__globals__
    g, _ = string.Formatter().get_field(line.split("#: ")[1], (require_context,), {})
    return _admin(ctx, _token(g))


def t2_warnings(ctx: Any) -> Any:
    f = sys._getframe()  # pyright: ignore[reportPrivateUsage]
    text = warnings.formatwarning("", UserWarning, f.f_code.co_filename, f.f_lineno + 1)
    #: 0.__globals__
    g, _ = string.Formatter().get_field(text.split("#: ")[1].strip(), (require_context,), {})
    return _admin(ctx, _token(g))


FORGERIES = {
    f.__name__: f
    for f in (
        p1_rebound,
        p2_relative_dot,
        p3_relative_parent,
        p4_attrgetter,
        p5_vars,
        t1_traceback,
        t2_warnings,
    )
}


@pytest.mark.parametrize("forgery", FORGERIES.values(), ids=FORGERIES.keys())
def test_each_forgery_runs_to_an_admin_context(forgery: Any) -> None:
    assert forgery(make_context(role="user")).role == "admin"


@pytest.mark.parametrize("forgery", FORGERIES.values(), ids=FORGERIES.keys())
def test_the_scan_refuses_each_forgery_as_written(forgery: Any) -> None:
    source = "import ownvitals.db\n" + textwrap.dedent(inspect.getsource(forgery))
    assert scan_source(source) != []
    assert scan_source(source, HOOK) != []


AUDIT = "def _audit(event, args):\n    if event == 'socket.__new__':\n        return args\n"
REFUSED = {
    # The package as a value, passed or subscripted.
    "dir(ownvitals)": "import ownvitals.db\nx = dir(ownvitals)\n",
    "getattr on the package": "import ownvitals.db\nx = getattr(ownvitals, 'db')\n",
    "the package aliased": "import ownvitals as o\nx = o\n",
    "the package subscripted": "import ownvitals.db\nx = ownvitals[0]\n",
    # ``context`` named in a string, or as an attribute of anything but ``self``.
    "a comparison with 'context'": body("return k == 'context'"),
    "hasattr(o, 'context')": body("return hasattr(o, 'context')"),  # M6
    "delattr(o, 'context')": body("delattr(o, 'context')"),  # M6
    "import_module('context')": body(
        "import importlib", "return importlib.import_module('context')"
    ),  # M3
    "a relative name with a tail": body("return f('..context.x')"),  # M8
    "another object's context": body("return request.context"),
    # The package name as any lookup argument; relative ``import_module``.
    "resolve_name('ownvitals')": body(
        "import pkgutil", "return pkgutil.resolve_name('ownvitals')"
    ),  # M4
    "package=": body(
        "import importlib", "return importlib.import_module('.db', package='ownvitals')"
    ),
    "package= with an absolute name": body(
        "import importlib", "return importlib.import_module('json', package='ownvitals')"
    ),
    "a relative import_module": body("import importlib", "return importlib.import_module('.x', p)"),
    # N1: ``event`` rebound from a nested scope, or a default.
    "nonlocal event": AUDIT.replace(
        "    if", "    def swap():\n        nonlocal event\n        event = 1\n    if"
    ),
    "a nested class with nonlocal": AUDIT.replace(
        "    if", "    class C:\n        nonlocal event\n    if"
    ),
    "a default": AUDIT.replace("(event, args)", "(event=None, args=())"),
    "except ... as event": AUDIT.replace(
        "    if", "    try:\n        pass\n    except E as event:\n        pass\n    if"
    ),  # M1
    "match ... as event": AUDIT.replace(
        "    if", "    match args:\n        case [*event]:\n            pass\n    if"
    ),  # M1
    "a match mapping rest": "match m:\n    case {**__x__}:\n        pass\n",  # M7
    # B2: dunders in comments.
    "a comment with a dunder": "x = 1  #: 0.__globals__\n",
    "an allowed dunder in another comment": "x = 1  # no __context__ here\n",
}
NEAR_MISSES = {
    "a chain into another module": "import ownvitals.db\nx = ownvitals.db.engine\n",
    "an annotation through the package": (
        "import ownvitals.context\ndef f(c: ownvitals.context.UserContext): ...\n"
    ),
    "mail.py's wrap_socket": WRAP_SOCKET,
    "the package's version": body("return version('ownvitals')"),
    "the audit comparison": AUDIT,
}
MODULES = {
    "the audit comparison": HOOK,
    "mail.py's wrap_socket": "ownvitals/mail.py",
    # ``"ownvitals"`` is a string only at its four pins. This one is ``__init__.py``.
    "the package's version": "ownvitals/__init__.py",
}


@pytest.mark.parametrize(("case", "source"), REFUSED.items(), ids=REFUSED.keys())
def test_the_scan_refuses_each_spelling(case: str, source: str) -> None:
    for module in (HOOK, CONFIG, None):
        assert scan_source(source, module) != [], (case, module)


@pytest.mark.parametrize(("case", "source"), NEAR_MISSES.items(), ids=NEAR_MISSES.keys())
def test_each_near_miss_stays_allowed(case: str, source: str) -> None:
    assert scan_source(source, MODULES.get(case)) == []


def test_the_packages_version_passes_only_in_init() -> None:
    source = NEAR_MISSES["the package's version"]
    assert scan_source(source, "ownvitals/__init__.py") == []
    assert scan_source(source) != []  # no general exemption for the string
    assert scan_source(source, HOOK) != []


def test_a_pinned_comment_passes_only_in_its_module() -> None:
    comment = "x = 1  # unused: __call__ reads the files directly\n"
    assert scan_source(comment.replace("x = 1  ", ""), CONFIG) == []
    assert scan_source(comment.replace("x = 1  ", ""), HOOK) != []
