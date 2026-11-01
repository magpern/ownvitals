"""Guard 5 of ADR 0002 section 7, continued (#75): the field writers.

A frozen ``UserContext`` refuses ``ctx.role = ...`` through its own ``__setattr__``, but
``object.__setattr__`` reached another way, a slot descriptor's ``__set__``, or a blank instance
from ``__new__`` writes a field anyway. Each function below is one of the #65 review's
constructions: it runs here on a real context, and ``user_context_scan`` must refuse its source.
"""

import dataclasses
import inspect
import textwrap
from collections.abc import Callable
from typing import Any

import pytest

from ctx import make_context
from ownvitals.context import UserContext, issuer_token, require_context
from user_context_scan import CONTEXT_FIELDS, scan_source


def super_with_arguments(ctx: Any) -> Any:
    super(type(ctx), ctx).__setattr__("role", "admin")
    return ctx


def descriptor_through_type(ctx: Any) -> Any:
    type(ctx).role.__set__(ctx, "admin")
    return ctx


def descriptor_through_class(ctx: Any) -> Any:
    ctx.__class__.role.__set__(ctx, "admin")
    return ctx


def blank_instance_and_descriptors(ctx: Any) -> Any:
    K: Any = type(ctx)
    new = K.__new__(K)
    K.user_id.__set__(new, ctx.user_id)
    K.role.__set__(new, "admin")
    K.connections.__set__(new, frozenset())
    return new


CONSTRUCTIONS: dict[str, Callable[[Any], Any]] = {
    "super(type(ctx), ctx).__setattr__": super_with_arguments,
    "type(ctx).role.__set__": descriptor_through_type,
    "ctx.__class__.role.__set__": descriptor_through_class,
    "K.__new__(K) and the slot descriptors": blank_instance_and_descriptors,
}


@pytest.mark.parametrize("construction", CONSTRUCTIONS.values(), ids=CONSTRUCTIONS.keys())
def test_each_construction_forges_an_admin_context_at_run_time(
    construction: Callable[[Any], Any],
) -> None:
    ctx = make_context(role="user")
    forged = construction(ctx)
    assert require_context(forged) is forged
    assert type(forged) is UserContext
    assert forged.role == "admin"


@pytest.mark.parametrize("construction", CONSTRUCTIONS.values(), ids=CONSTRUCTIONS.keys())
def test_the_scan_refuses_each_construction(construction: Callable[[Any], Any]) -> None:
    assert scan_source(textwrap.dedent(inspect.getsource(construction))) != []


def body(*statements: str) -> str:
    return "def f(ctx, K):\n" + "".join(f"    {statement}\n" for statement in statements)


# Relatives of the constructions above: the same writes, spelled another way.
REFUSED = {
    "super() without arguments": body("super().__setattr__('role', 'admin')"),
    "super(...).__delattr__": body("super(type(ctx), ctx).__delattr__('role')"),
    "object.__setattr__ through __mro__": body(
        "type(ctx).__mro__[-1].__setattr__(ctx, 'role', 'admin')"
    ),
    "object.__setattr__ through __base__": body(
        "ctx.__class__.__base__.__setattr__(ctx, 'role', 'admin')"
    ),
    "type.__setattr__": body("type.__setattr__(K, 'role', 1)"),
    "getattr of __setattr__": body("getattr(super(K, ctx), '__setattr__')('role', 'admin')"),
    "methodcaller of __setattr__": body(
        "import operator", "operator.methodcaller('__setattr__', 'role', 'admin')(ctx)"
    ),
    "a descriptor taken into a name": body("d = K.role", "d.__set__(ctx, 'admin')"),
    "a descriptor from vars": body("vars(K)['role'].__set__(ctx, 'admin')"),
    "getattr of __set__": body("getattr(K.role, '__set__')(ctx, 'admin')"),
    "__delete__": body("K.role.__delete__(ctx)"),
    "getattr of __delete__": body("getattr(K.role, '__delete__')(ctx)"),
    "a class body naming __setattr__": "class A:\n    def __setattr__(self, n, v): ...\n"
    "    __delattr__ = __setattr__\n",
    "K.__new__(K)": body("return K.__new__(K)"),
    "__new__ taken as a value": body("n = K.__new__", "return n(K)"),
    "getattr of __new__": body("return getattr(K, '__new__')(K)"),
    "super().__new__ outside __new__": "class A:\n    def make(cls):\n"
    "        return super().__new__(cls)\n",
    "super(...).__new__ with arguments in __new__": "class A:\n    def __new__(cls):\n"
    "        return super(A, cls).__new__(cls)\n",
    "a class body naming __new__": "class A:\n    def __new__(cls): ...\n    make = __new__\n",
}
ALLOWED = {
    "super().__new__ in __new__": "class S(Base):\n    def __new__(cls, *args):\n"
    "        return super().__new__(cls)\n",
    "defining __setattr__ and __delattr__": "class Frozen:\n"
    "    def __setattr__(self, name, value):\n        raise AttributeError(name)\n"
    "    def __delattr__(self, name):\n        raise AttributeError(name)\n",
    "defining a descriptor": "class D:\n    def __set__(self, obj, value): ...\n"
    "    def __delete__(self, obj): ...\n",
}


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_every_other_spelling_of_a_field_write_is_refused(source: str) -> None:
    assert scan_source(source) != []


@pytest.mark.parametrize("source", ALLOWED.values(), ids=ALLOWED.keys())
def test_defining_the_methods_and_super_new_in_new_stay_allowed(source: str) -> None:
    assert scan_source(source) == []


# The #81 review's N2 (#89): three more constructions, each run here on a real context.
def token_through_globals(ctx: Any) -> Any:
    token = next(v for v in require_context.__globals__.values() if type(v) is object)
    K = type(ctx)
    return K(ctx.user_id, "admin", issuer=token)


def idiom_called_through_vars(ctx: Any) -> Any:
    class A:
        def __post_init__(self) -> None:
            object.__setattr__(self, "role", "admin")

    for hook in vars(A).values():
        if callable(hook):
            hook(ctx)
    return ctx


def blank_through_copyreg(ctx: Any) -> Any:
    import copyreg

    return copyreg.__newobj__(type(ctx))  # pyright: ignore[reportAttributeAccessIssue]


FORGERIES: dict[str, Callable[[Any], Any]] = {
    "the token through __globals__": token_through_globals,
    "the idiom called through vars()": idiom_called_through_vars,
}


@pytest.mark.parametrize("forgery", FORGERIES.values(), ids=FORGERIES.keys())
def test_the_review_forgeries_build_an_admin_context_at_run_time(
    forgery: Callable[[Any], Any],
) -> None:
    forged = forgery(make_context(role="user"))
    assert require_context(forged) is forged
    assert forged.role == "admin"


def test_copyreg_builds_a_blank_context_without_the_token() -> None:
    blank = blank_through_copyreg(make_context(role="user"))
    assert require_context(blank) is blank
    with pytest.raises(AttributeError):
        _ = blank.role


@pytest.mark.parametrize(
    "construction",
    [*FORGERIES.values(), blank_through_copyreg],
    ids=[*FORGERIES.keys(), "copyreg.__newobj__"],
)
def test_the_scan_refuses_each_review_forgery(construction: Callable[[Any], Any]) -> None:
    assert scan_source(textwrap.dedent(inspect.getsource(construction))) != []


def test_the_scan_knows_every_field_of_the_context() -> None:
    assert {field.name for field in dataclasses.fields(UserContext)} == CONTEXT_FIELDS


def post_init(*statements: str) -> str:
    return "class A:\n    def __post_init__(self):\n" + "".join(
        f"        {statement}\n" for statement in statements
    )


# Rules (a) to (d) of #89: each refused spelling, and a near miss that stays allowed.
RULES: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "__globals__": (
        {
            "the attribute": body("return f.__globals__"),
            "getattr": body("return getattr(f, '__globals__')"),
            "from vars": body("return vars(f)['__globals__']"),
            "a bare name": body("return __globals__"),
        },
        {
            "globals()": body("return globals()['X']"),
            "an attribute named globals": body("return f.globals"),
            "a string that is not the name": body("return '__globals'"),
        },
    ),
    "issuer=": (
        {
            "a call": body("return K(ctx.user_id, 'admin', issuer=t)"),
            "functools.partial": body("import functools", "functools.partial(K, issuer=t)"),
            "a ** literal": body("return K(ctx.user_id, 'admin', **{'issuer': t})"),
            "dict(issuer=...)": body("return K(ctx.user_id, 'admin', **dict(issuer=t))"),
            "a dict comprehension": body("return K(1, 'admin', **{k: t for k in ['issuer']})"),
            "dict([...])": body("return K(1, 'admin', **dict([('issuer', t)]))"),
            "a dict union": body("return K(1, 'admin', **({} | {'issuer': t}))"),
        },
        {
            "another keyword": body("return f(issuer_url=1, iss=2)"),
            "a ** literal without it": body("return f(**{'iss': 1})"),
            "a ** name": body("return f(**options)"),
            "a ** comprehension without it": body("return f(**{k: 1 for k in ['iss']})"),
            "the word as a value": body("return config['issuer']"),
        },
    ),
    "__module__": (
        {
            "the attribute": body("import sys", "return sys.modules[f.__module__]"),
            "a string": body("return getattr(f, '__module__')"),
        },
        {"__name__": body("return f.__name__"), "a similar name": body("return f.__modules")},
    ),
    "the module ownvitals.context": (
        {
            "import as": "import ownvitals.context as c\n",
            "from the package": "from ownvitals import context\n",
            "from the package, aliased": "from ownvitals import context as c\n",
            "relative": "from .. import context\n",
            "relative, one level": "from . import context as c\n",
            "the package attribute": "import ownvitals.db\nm = ownvitals.context\n",
            "the package aliased": "import ownvitals as o\nm = o.context\n",
            "sys.modules": body("import sys", "return sys.modules['ownvitals.context']"),
            "sys.modules.get": body("import sys", "return sys.modules.get('ownvitals.context')"),
            "import_module": body(
                "import importlib", "return importlib.import_module('ownvitals.context')"
            ),
            "import_module, relative": body(
                "import importlib", "return importlib.import_module('.context', 'ownvitals')"
            ),
            "__import__": body("return __import__('ownvitals.context')"),
        },
        {
            "names from the module": "from ownvitals.context import require_context\n",
            "relative names": "from ..context import require_context\n",
            "a plain import for annotations": "import ownvitals.context\n"
            "def f(ctx: ownvitals.context.UserContext): ...\n",
            "another module of the package": "from ownvitals import db\nimport ownvitals.db\n"
            "x = ownvitals.db\n",
            "another object's context": body("return request.context"),
            "a similar string": body("return 'ownvitals.contexts'"),
        },
    ),
    "the idiom on a context field": (
        {
            "role": post_init("object.__setattr__(self, 'role', 'admin')"),
            "user_id": post_init("object.__setattr__(self, 'user_id', 1)"),
            "connections": post_init("object.__setattr__(self, 'connections', frozenset())"),
            "in another method": "class A:\n    def hook(self):\n"
            "        object.__setattr__(self, 'role', 'admin')\n",
        },
        {
            "another field": post_init("object.__setattr__(self, 'roles', ())"),
            "a field with a context name as a prefix": post_init(
                "object.__setattr__(self, 'user_id_hash', b'')"
            ),
        },
    ),
    "copyreg": (
        {
            "import": "import copyreg\n",
            "import as": "import copyreg as c\n",
            "from import": "from copyreg import __newobj__\n",
            "a string": body("import importlib", "return importlib.import_module('copyreg')"),
            "sys.modules": body("import sys", "return sys.modules['copyreg']"),
            "an attribute": body("return m.copyreg"),
        },
        {
            "copy": "import copy\nfrom copy import deepcopy\n",
            "pickle": "import pickle\n",
            "a similar name": body("return copyregistry.x"),
        },
    ),
}
REFUSED_BY_RULE = {f"{rule}: {k}": v for rule, (r, _) in RULES.items() for k, v in r.items()}
ALLOWED_BY_RULE = {f"{rule}: {k}": v for rule, (_, a) in RULES.items() for k, v in a.items()}


@pytest.mark.parametrize("source", REFUSED_BY_RULE.values(), ids=REFUSED_BY_RULE.keys())
def test_each_new_rule_refuses_its_spellings(source: str) -> None:
    assert scan_source(source) != []


@pytest.mark.parametrize("source", ALLOWED_BY_RULE.values(), ids=ALLOWED_BY_RULE.keys())
def test_each_new_rule_leaves_its_near_misses_alone(source: str) -> None:
    assert scan_source(source) == []


# The #115 review's forgery (B1) and its variants: the token through the module's namespace,
# each with a near miss of the same shape that reaches no token.
def escalate(ctx: Any) -> Any:
    from ownvitals import context as c

    tok = next(v for v in vars(c).values() if type(v) is object)
    kw = {"issuer": tok}
    K = type(ctx)
    return c.require_context(K(ctx.user_id, "admin", **kw))


FORGERY = textwrap.dedent(inspect.getsource(escalate))
NEAR_MISS = """\
from ownvitals.context import require_context


def harmless(ctx, options, obj):
    first = next(v for v in vars(obj).values() if type(v) is object)
    kw = {"iss": first}
    return require_context(make(ctx.user_id, **kw))
"""
SYS_MODULES = ("vars(c)", "vars(sys.modules['ownvitals.context'])")
MODULE_ROUTES = {
    "from ownvitals import context": (FORGERY, NEAR_MISS),
    "module.__dict__": (
        FORGERY.replace("vars(c)", "c.__dict__"),
        NEAR_MISS.replace("vars(obj)", "obj.__dict__"),
    ),
    "sys.modules": (
        FORGERY.replace("from ownvitals import context as c", "import sys")
        .replace(*SYS_MODULES)
        .replace("c.require_context", "require_context"),
        NEAR_MISS.replace("vars(obj)", "vars(sys.modules['ownvitals.db'])"),
    ),
    "a dict comprehension": (
        FORGERY.replace("**kw", "**{k: tok for k in ['issuer']}"),
        NEAR_MISS.replace("**kw", "**{k: first for k in ['iss']}"),
    ),
    "dict([...])": (
        FORGERY.replace("**kw", "**dict([('issuer', tok)])"),
        NEAR_MISS.replace("**kw", "**dict([('iss', first)])"),
    ),
}


def test_the_review_forgery_builds_an_admin_context_at_run_time() -> None:
    forged = escalate(make_context(role="user"))
    assert require_context(forged) is forged
    assert forged.role == "admin"


@pytest.mark.parametrize(("forgery", "_"), MODULE_ROUTES.values(), ids=MODULE_ROUTES.keys())
def test_the_scan_refuses_the_module_namespace_routes(forgery: str, _: str) -> None:
    assert scan_source(forgery) != []


@pytest.mark.parametrize(("_", "near_miss"), MODULE_ROUTES.values(), ids=MODULE_ROUTES.keys())
def test_their_near_misses_stay_allowed(_: str, near_miss: str) -> None:
    assert scan_source(near_miss) == []


# The second #115 review (#89): annotations run at import time on Python 3.12, so the token
# routes are refused inside them too, and an annotation may only name a type.
TOKEN_KEY = "_ISSUER"  # a name, so the linter doesn't read the string as a forward reference


def annotation_reaches_the_token() -> object:
    def f(t: require_context.__globals__[TOKEN_KEY]) -> None: ...  # pyright: ignore

    return f.__annotations__["t"]


def test_an_annotation_can_reach_the_token_at_run_time() -> None:
    assert annotation_reaches_the_token() is issuer_token()


ANNOTATION_ROUTES: dict[str, tuple[str, str]] = {
    "__globals__ in a parameter annotation": (
        "from ownvitals.context import require_context\n"
        "def f(t: require_context.__globals__['_ISSUER']) -> None: ...\n",
        "from ownvitals.context import require_context\ndef f(t: dict[str, object]) -> None: ...\n",
    ),
    "the token through a plain import": (
        "import ownvitals.context\ndef f(t: ownvitals.context._ISSUER) -> None: ...\n",
        "import ownvitals.context\ndef f(t: ownvitals.context.UserContext) -> None: ...\n",
    ),
    "issuer_token() in a variable annotation": (
        "import ownvitals.context\ntok: ownvitals.context.issuer_token() = None\n",
        "import ownvitals.context\nctx: ownvitals.context.UserContext | None = None\n",
    ),
    "issuer= in a return annotation": (
        "def f(c, t) -> (lambda: K(1, 'admin', issuer=t)): ...\n",
        "def f(c, t) -> Callable[[int], K]: ...\n",
    ),
    "**{'issuer': ...} in a variable annotation": (
        "v: K(1, 'a', **{'issuer': t}) = 1\n",
        "v: Annotated[K, Field(alias='a')] = 1\n",
    ),
    "a call": ("def f(x: make()) -> None: ...\n", "def f(x: make) -> None: ...\n"),
    "a subscript of a non-type": (
        "def f(x: [int][0]) -> None: ...\n",
        "def f(x: tuple[int, ...]) -> None: ...\n",
    ),
    "an attribute starting with _": (
        "def f(x: m._hidden) -> None: ...\n",
        "def f(x: m.Visible) -> None: ...\n",
    ),
    "a lambda": ("def f(x: (lambda: int)) -> None: ...\n", "def f(x: int | None) -> None: ...\n"),
    "the token's name as a string": (
        "x: '_ISSUER' = None\n",
        "x: Literal['user', 'admin'] = 'user'\n",
    ),
    "the token's name in Literal": (
        "x: Literal['_ISSUER'] = None\n",
        "x: Literal['_user'] = None\n",
    ),
    "issuer= in Annotated's metadata": (
        "def f(x: Annotated[int, Field(issuer=t)]): ...\n",
        "def f(x: Annotated[int, Field(alias='a')]): ...\n",
    ),
    "a whole-string annotation": (
        "def f(x: 'require_context.__globals__[\"_ISSUER\"]') -> None: ...\n",
        "def f(x: 'dict[str, UserContext]') -> None: ...\n",
    ),
    "Annotated's metadata under an alias": (
        "from typing import Annotated as A\ndef f(x: A[int, Depends(issuer_token)]): ...\n",
        "from typing import Annotated as A\nfrom fastapi import Depends\n"
        "def f(x: A[int, Depends(get)]): ...\n",
    ),
    "a star import from ownvitals": ("from ownvitals import *\n", "from os.path import *\n"),
    "a star import from a subpackage": (
        "from ownvitals.db import *\n",
        "from ownvitals.db import x\n",
    ),
    "a relative star import": ("from .. import *\n", "from .. import db\n"),
    "a relative star import, one level": ("from .jobs import *\n", "from .jobs import run\n"),
    "__kwdefaults__": (body("f.__kwdefaults__['issuer'] = t"), body("return f.__doc__")),
    "__defaults__": (body("f.__defaults__ = (t,)"), body("return f.defaults")),
}


@pytest.mark.parametrize(("route", "_"), ANNOTATION_ROUTES.values(), ids=ANNOTATION_ROUTES.keys())
def test_the_scan_refuses_the_annotation_and_star_import_routes(route: str, _: str) -> None:
    assert scan_source(route) != []


@pytest.mark.parametrize(("_", "near"), ANNOTATION_ROUTES.values(), ids=ANNOTATION_ROUTES.keys())
def test_the_annotation_and_star_import_near_misses_stay_allowed(_: str, near: str) -> None:
    assert scan_source(near) == []
