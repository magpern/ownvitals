"""Guard 5's attribute rule, continued (#89, the Architect's fixes for the tenth #115 review):
an allowed dunder inside a string is pinned to one exact constant in one syntactic position;
``inspect`` and ``pydoc`` are refused, and docstrings are judged for the token's names and the
context module; ``:`` counts like ``.`` in a module path.

The review's forgeries run here to an admin context, and the scan must refuse each one's source.
"""

import inspect
import pkgutil
import string
import textwrap
from typing import Any

import pytest

from ctx import make_context
from ownvitals.context import require_context
from test_user_context_writers import body
from user_context_scan import scan_source

HOOK = "ownvitals/egress/hook.py"
LOG = "ownvitals/log.py"


def _admin(ctx: Any, tok: object) -> Any:
    kw = {"issuer": tok}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


def _token(namespace: dict[str, Any]) -> object:
    return next(v for v in namespace.values() if type(v) is object)


# B2: ``inspect`` hands a docstring or a comment back as a value.
def docstring_path(ctx: Any) -> Any:
    def h() -> None:
        "ownvitals.context:_ISSUER"

    return _admin(ctx, pkgutil.resolve_name(inspect.getdoc(h) or ""))


def docstring_field(ctx: Any) -> Any:
    def h() -> None:
        "0.__globals__"

    g, _ = string.Formatter().get_field(inspect.getdoc(h) or "", (require_context,), {})
    return _admin(ctx, _token(g))


# ownvitals.context:_ISSUER
def _commented() -> None: ...


def comment_path(ctx: Any) -> Any:
    path = (inspect.getcomments(_commented) or "")[2:].strip()
    return _admin(ctx, pkgutil.resolve_name(path))


# B3: ``pkgutil``'s colon between the package and the module.
def colon_module(ctx: Any) -> Any:
    m = pkgutil.resolve_name("ownvitals:context")
    return _admin(ctx, _token(vars(m)))


FORGERIES = {f.__name__: f for f in (docstring_path, docstring_field, comment_path, colon_module)}


@pytest.mark.parametrize("forgery", FORGERIES.values(), ids=FORGERIES.keys())
def test_each_forgery_runs_to_an_admin_context(forgery: Any) -> None:
    assert forgery(make_context(role="user")).role == "admin"


@pytest.mark.parametrize("forgery", FORGERIES.values(), ids=FORGERIES.keys())
def test_the_scan_refuses_each_forgery_as_written(forgery: Any) -> None:
    source = textwrap.dedent(inspect.getsource(forgery))
    if forgery is comment_path:
        source = "import inspect\n" + source  # the module's import, outside the function
    assert scan_source(source) != []
    assert scan_source(source, HOOK) != []


# B1: the audit event's string, derived into a name that ``setattr`` writes.
PARTITION = """\
stash = []
def install(ctx):
    n = "socket.__new__".partition(".")[2]
    K = type(ctx)
    base = getattr(object, n)
    def grab(cls, *a, **kw):
        stash.append(kw)
        return base(cls)
    setattr(K, n, grab)
"""
REFUSED = {
    "the partition route": PARTITION,
    "the event name passed on": body("return audit('socket.__new__')"),
    "the event name sliced": body("return 'socket.__new__'[7:]"),
    "the event name in another comparison": body("return name == 'socket.__new__'"),
    "__main__ passed on": body("return getattr(sys.modules['__main__'], 'x')"),
    "__main__ in another comparison": body("return x == '__main__'"),
    "a placeholder elsewhere": body("return '__NAMES__'.strip('_')"),
    # B2: the readers, under any spelling; a docstring naming the token or the module.
    "import inspect": "import inspect\n",
    "inspect aliased": "import inspect as i\n",
    "from inspect": "from inspect import getdoc\n",
    "import pydoc": "import pydoc\n",
    "from pydoc": "from pydoc import getdoc as g\n",
    "inspect by import_module": body("import importlib", "return importlib.import_module('pydoc')"),
    "a docstring naming the token": "def h():\n    '''See _ISSUER.'''\n",
    "a docstring with the module": "def h():\n    '''ownvitals.context:require_context'''\n",
    # B3: ``:`` like ``.`` everywhere.
    "ownvitals:context": body("return resolve_name('ownvitals:context')"),
    "a path after ownvitals:context": body("return resolve_name('ownvitals:context.require')"),
    "the module named exactly": body("return resolve_name('ownvitals.context')"),
    "the module after a prefix": body("return f('see ownvitals:context')"),
    # V5: a keyword argument is no read.
    "a dunder keyword": body("return f(__name__=x)"),
}
NEAR_MISSES = {
    "the event comparison in hook.py": body("return event == 'socket.__new__'"),
    "the entry point": "if __name__ == '__main__':\n    main()\n",
    "another module of the package": body("return resolve_name('ownvitals:db')"),
    "a longer name": body("return getLogger('ownvitals.contexts')"),
    "a docstring about the package": "def h():\n    '''Reads ``ownvitals.db``.'''\n",
}


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_the_scan_refuses_each_spelling(source: str) -> None:
    assert scan_source(source, HOOK) != []


@pytest.mark.parametrize("source", NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_each_near_miss_stays_allowed(source: str) -> None:
    assert scan_source(source, HOOK) == []


def test_the_placeholders_are_pinned_to_their_one_assignment() -> None:
    sql = "QUEUE_QUERY = 'SELECT 1 FROM t WHERE x IN (__NAMES__)'.replace('__NAMES__', n)\n"
    assert scan_source(sql, "ownvitals/db/queue_check.py") == []
    assert scan_source(sql.replace("QUEUE_QUERY", "OTHER"), "ownvitals/db/queue_check.py") != []


# V2: deleting an attribute is a write even where the name may be read.
def test_deleting_an_allowed_dunder_is_refused_where_it_is_allowed() -> None:
    assert scan_source(body("return record.__dict__"), LOG) == []
    assert scan_source(body("del record.__dict__"), LOG) != []
