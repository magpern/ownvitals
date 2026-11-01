"""Guard 5's attribute rule, continued (#89, the Architect's fixes for the ninth #115 review):
every identifier-shaped segment of every ``str`` and ``bytes`` constant is judged, and an
allowed dunder is read, never written.

The review's forgeries run here to an admin context, and the scan must refuse each one's source.
"""

import inspect
import operator
import pkgutil
import pydoc
import string
import textwrap
import types
from typing import Any

import pytest

from ctx import make_context
from ownvitals.context import require_context
from test_user_context_writers import body
from user_context_dunders import beyond_the_allow_list
from user_context_scan import scan_source

HOOK = "ownvitals/egress/hook.py"


def _admin(ctx: Any, tok: object) -> Any:
    kw = {"issuer": tok}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


def _token(namespace: dict[str, Any]) -> object:
    return next(v for v in namespace.values() if type(v) is object)


# B1: standard-library calls that look a dotted or colon path up themselves.
def formatter_field(ctx: Any) -> Any:
    g, _ = string.Formatter().get_field("0.__globals__", (require_context,), {})
    return _admin(ctx, _token(g))


def attrgetter_path(ctx: Any) -> Any:
    g = operator.attrgetter("f.__globals__")(types.SimpleNamespace(f=require_context))
    return _admin(ctx, _token(g))


def bytes_name(ctx: Any) -> Any:
    g = getattr(require_context, b"__globals__".decode())
    return _admin(ctx, _token(g))


def resolve_name(ctx: Any) -> Any:
    return _admin(ctx, pkgutil.resolve_name("ownvitals.context:_ISSUER"))


def locate(ctx: Any) -> Any:
    return _admin(ctx, pydoc.locate("ownvitals.context._ISSUER"))


# B2 (R8): a function's code replaced; ``require_context`` then accepts anything. Restored after.
def code_swap(ctx: Any) -> Any:
    original = require_context.__code__
    try:
        require_context.__code__ = (lambda c: c).__code__
        return require_context(types.SimpleNamespace(user_id=ctx.user_id, role="admin"))
    finally:
        require_context.__code__ = original


FORGERIES = {
    f.__name__: f for f in (formatter_field, attrgetter_path, bytes_name, resolve_name, locate)
}


@pytest.mark.parametrize("forgery", [*FORGERIES.values(), code_swap], ids=[*FORGERIES, "code"])
def test_each_forgery_runs_to_an_admin_context(forgery: Any) -> None:
    assert forgery(make_context(role="user")).role == "admin"


@pytest.mark.parametrize("forgery", [*FORGERIES.values(), code_swap], ids=[*FORGERIES, "code"])
def test_the_scan_refuses_each_forgery_as_written(forgery: Any) -> None:
    source = textwrap.dedent(inspect.getsource(forgery))
    assert scan_source(source) != []
    assert scan_source(source, HOOK) != []  # ``hook.py``'s exceptions don't open it either


# R7: the class's own ``__init__`` rewritten (scan only: it would change the class for good).
CODE_REWRITE = body(
    "for f in vars(type(ctx)).values():",
    "    code = getattr(f, '__code__', None)",
    "    if code is not None and code.co_kwonlyargcount == 1:",
    "        f.__code__ = code.replace(co_names=code.co_names)",
)
REFUSED = {
    "a format field": body("return '{0.__module__}'.format(type(ctx))"),
    "an attrgetter path": body("import operator", "return operator.attrgetter('a.__dict__')(x)"),
    "bytes": body("return getattr(f, b'__globals__'.decode())"),
    "a bytes path": body("return attrgetter(b'f.__globals__'.decode())(x)"),
    "the token in a colon path": body("return resolve_name('ownvitals.context:_ISSUER')"),
    "the token in a dotted path": body("return locate('ownvitals.context._ISSUER')"),
    "the token in another module's path": body("return locate('somewhere.issuer_token')"),
    "a path into the context module": body("return locate('ownvitals.context.require_context')"),
    "a colon path into the context module": body(
        "return resolve_name('ownvitals.context:require_context')"
    ),
    "f-string text": body("return f'{x}.__globals__'"),
    "the class's __init__ rewritten": CODE_REWRITE,
    # Writes of an allowed dunder: none is allowed.
    "assigning __code__": body("require_context.__code__ = g.__code__"),
    "assigning __name__": body("f.__name__ = 'x'"),
    "augmenting __name__": body("f.__name__ += 'x'"),
    "deleting __dict__": body("del record.__dict__"),
    "setattr of __code__": body("setattr(f, '__code__', c)"),
    "delattr of __name__": body("delattr(f, '__name__')"),
    "another getattr of __code__": body("return getattr(require_context, '__code__')"),
    "deleting __all__": "del __all__\n",
    "setattr of a string-allowed dunder": body("setattr(socket, '__new__', f)"),
    "delattr of a string-allowed dunder": body("delattr(m, '__main__')"),
    "__name__ as a string": body("return getattr(f, '__name__')"),
}
NEAR_MISSES = {
    "a dotted logger name": body("return getLogger('ownvitals.db.engine')"),
    "SQL text": body("return text('SELECT a.b FROM t WHERE c = :c')"),
    "a format field": body("return '{0.name}'.format(x)"),
    "an attrgetter path": body("import operator", "return operator.attrgetter('a.b')(x)"),
    "bytes": body("return b'abc.def'"),
    "a docstring": "def f():\n    '''Reads ``x.__globals__``, ``ownvitals.context._ISSUER``.'''\n",
    "an allowed read": body("return f.__name__"),
    "defining __all__": "__all__ = ['f']\n",
    "a name like the module's": body("return locate('ownvitals.contexts.other')"),
}


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_the_scan_refuses_each_spelling(source: str) -> None:
    assert scan_source(source, HOOK) != []


@pytest.mark.parametrize("source", NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_each_near_miss_stays_allowed(source: str) -> None:
    assert scan_source(source) == []


def test_hooks_socketpair_read_is_the_one_allowed_code_read() -> None:
    read = "import socket\nCODE = getattr(socket.socketpair, '__code__', None)\n"
    assert scan_source(read, HOOK) == []
    assert scan_source(read) != []  # only in ``hook.py``


def test_hooks_audit_event_name_is_allowed_only_there() -> None:
    event = body("return event == 'socket.__new__'")
    assert scan_source(event, HOOK) == []
    assert scan_source(event) != []


# The ninth review's N5: each site alone, so a mutation that drops it is seen.
@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("import email.__x__\n", 1),  # a dotted part
        ("import builtins as __builtins__\n", 1),  # the ``as`` name
        # ``nonlocal``, bound by a parameter (a parameter names no attribute)
        ("def f(__x__):\n    def g():\n        nonlocal __x__\n", 3),
    ],
    ids=["a dotted import part", "an import's as name", "nonlocal"],
)
def test_each_binding_site_alone_is_refused(source: str, line: int) -> None:
    found = scan_source(source)
    assert [where for where, _ in found] == [line]
    assert beyond_the_allow_list(found) == []
