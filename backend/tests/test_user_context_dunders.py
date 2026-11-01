"""Guard 5's attribute rule (#89, the Architect's decision on the eighth #115 review): dunder
names are an allow-list (``user_context_dunders``), and ``import_module`` takes only a literal.

The eighth review's forgeries run here to an admin context, and the scan must refuse each one's
source. The allow-list is pinned to what ``src`` uses: no more entries, no wider ``only_in``.
"""

import ast
import importlib
import inspect
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ctx import make_context
from ownvitals.context import require_context
from test_user_context_writers import body
from user_context_dunders import ALLOWED, beyond_the_allow_list, is_dunder, named_dunders
from user_context_scan import ISSUER_MODULES, scan_source

SRC = Path(__file__).resolve().parent.parent / "src"


def _token(namespace: dict[str, Any]) -> object:
    return next(v for v in namespace.values() if type(v) is object)


# B1: a ``match`` class pattern reads an attribute by its keyword.
def match_globals(ctx: Any) -> Any:
    match require_context:
        case object(__globals__=g):
            tok = _token(g)
        case _:
            raise AssertionError
    kw = {"issuer": tok}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


def match_module(ctx: Any) -> Any:
    match require_context:
        case object(__module__=name):
            m = importlib.import_module(name)
        case _:
            raise AssertionError
    kw = {"issuer": _token(vars(m))}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


def match_setattr(ctx: Any) -> Any:
    match object:
        case type(__setattr__=s):
            s(ctx, "role", "admin")
        case _:
            raise AssertionError
    return require_context(ctx)


# B2: a comparison hands ``__module__`` to the other operand's ``__eq__`` or ``__contains__``.
class R(list[str]):
    __eq__ = list.append  # pyright: ignore  # keeps what it is compared with


def compare_equal(ctx: Any) -> Any:
    r = R()
    require_context.__module__ == r  # noqa: B015  # pyright: ignore
    m = importlib.import_module(r[0])
    kw = {"issuer": _token(vars(m))}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


def compare_in_list(ctx: Any) -> Any:
    r = R()
    type(ctx).__module__ in [r]  # noqa: B015  # pyright: ignore
    mods = sys.modules
    kw = {"issuer": _token(vars(mods[r[0]]))}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


def compare_in_mock(ctx: Any) -> Any:
    d = MagicMock()
    type(ctx).__module__ in d  # noqa: B015  # pyright: ignore
    m = importlib.import_module(d.mock_calls[0].args[0])
    kw = {"issuer": _token(vars(m))}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


# N1: the module name without ``__module__``.
def repr_name(ctx: Any) -> Any:
    m = importlib.import_module(repr(type(ctx)).split("'")[1].rpartition(".")[0])
    kw = {"issuer": _token(vars(m))}
    return require_context(type(ctx)(ctx.user_id, "admin", **kw))


FORGERIES = {
    f.__name__: f
    for f in (
        match_globals,
        match_module,
        match_setattr,
        compare_equal,
        compare_in_list,
        compare_in_mock,
        repr_name,
    )
}


@pytest.mark.parametrize("forgery", FORGERIES.values(), ids=FORGERIES.keys())
def test_each_forgery_runs_to_an_admin_context(forgery: Any) -> None:
    assert forgery(make_context(role="user")).role == "admin"


@pytest.mark.parametrize("forgery", FORGERIES.values(), ids=FORGERIES.keys())
def test_the_scan_refuses_each_forgery_as_written(forgery: Any) -> None:
    assert scan_source(textwrap.dedent(inspect.getsource(forgery))) != []


RECORDER = "class R(list):\n    __eq__ = list.append\n"
REFUSED = {
    # Every place a dunder can name an attribute.
    "an attribute": body("return f.__globals__"),
    "a name": body("return __builtins__"),
    "a string": body("return vars(K)['__init__']"),
    "getattr": body("return getattr(f, '__module__')"),
    "setattr": body("setattr(ctx, '__dict__', {})"),
    "hasattr": body("return hasattr(f, '__wrapped__')"),
    "delattr": body("delattr(K, '__slots__')"),
    "a match class-pattern keyword": body(
        "match f:", "    case object(__globals__=g):", "        return g"
    ),
    "a keyword argument": body("return f(__globals__=1)"),
    "an f-string": body("return f'{K.__qualname__}'"),
    "f-string text": body("return f'__module__{x}'"),
    "an imported name": "from ownvitals.context import __loader__\n",
    "a global declaration": body("global __builtins__"),
    "a class-body assignment": RECORDER,
    "an attribute in a comparison": body("return x.__module__ == 'asyncio'"),
    "a mixed comparison chain": body("return 'a' == x.__module__ < 'z'"),
    "a tuple operand": body("return (x.__module__,) == r"),
    "__import__": body("return __import__('json')"),
    "a dunder only_in another module": body("return record.__dict__"),
    # N1: a module name built at run time.
    "import_module with a name": body("import importlib", "return importlib.import_module(n)"),
    "import_module by keyword": body("import importlib", "return importlib.import_module(name=n)"),
    "import_module imported": body(
        "from importlib import import_module", "return import_module(n)"
    ),
    "import_module aliased": body("from importlib import import_module as im", "return im(n)"),
    "import_module taken as a value": body(
        "import importlib", "load = importlib.import_module", "return load('json')"
    ),
    "the module aliased": body("import importlib as il", "return il.import_module(f'{n}')"),
}
ALLOWED_SOURCES = {
    "a match keyword that is no dunder": body(
        "match p:", "    case Point(x=0):", "        return p"
    ),
    "defining dunder methods": "class P:\n    def __eq__(self, other): ...\n"
    "    def __contains__(self, x): ...\n",
    "an allow-listed dunder": body("return f.__name__"),
    "a private name": body("return f._cache__"),
    "the loop's class": body("return type(loop) is asyncio.SelectorEventLoop"),
    "import_module with a literal": body(
        "import importlib", "return importlib.import_module('json')"
    ),
    "import_module with a literal keyword": body(
        "from importlib import import_module as im", "return im(name='json')"
    ),
}


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_the_scan_refuses_each_spelling(source: str) -> None:
    assert scan_source(source) != []


@pytest.mark.parametrize("source", ALLOWED_SOURCES.values(), ids=ALLOWED_SOURCES.keys())
def test_each_near_miss_stays_allowed(source: str) -> None:
    assert scan_source(source) == []


def test_a_dunder_is_refused_by_the_allow_list_itself() -> None:
    """The match keyword and the comparison are refused by the dunder rule, not by luck."""
    for case in ("a match class-pattern keyword", "an attribute in a comparison", "setattr"):
        found = scan_source(REFUSED[case])
        assert found != [], case
        assert beyond_the_allow_list(found) == [], case


def test_only_in_is_honoured() -> None:
    source = body("return record.__dict__")
    assert scan_source(source, "ownvitals/log.py") == []
    assert scan_source(source, "ownvitals/web.py") != []


def _src_dunders() -> dict[str, set[str]]:
    """Each dunder ``src`` names (issuer modules aside), with the modules that name it."""
    used: dict[str, set[str]] = {}
    for path in sorted((SRC / "ownvitals").rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        if module in ISSUER_MODULES:
            continue
        for _, name in named_dunders(ast.parse(path.read_text("utf-8"))):
            used.setdefault(name, set()).add(module)
    return used


def test_the_allow_list_holds_exactly_what_src_uses() -> None:
    used = _src_dunders()
    assert set(ALLOWED) == set(used)
    for name, entry in ALLOWED.items():
        if entry.only_in is not None:
            assert entry.only_in == used[name], name
        else:
            assert len(used[name]) > 1, f"{name} is used in one module: give it only_in"


def test_a_dunder_is_two_underscores_each_side() -> None:
    assert is_dunder("__module__")
    assert is_dunder("__NAMES__")
    assert not is_dunder("_private")
    assert not is_dunder("__mangled")
    assert not is_dunder("____")
