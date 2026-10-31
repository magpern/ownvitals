"""Guard 5 of ADR 0002 section 7, continued (#75): the field writers.

A frozen ``UserContext`` refuses ``ctx.role = ...`` through its own ``__setattr__``, but
``object.__setattr__`` reached another way, a slot descriptor's ``__set__``, or a blank instance
from ``__new__`` writes a field anyway. Each function below is one of the #65 review's
constructions: it runs here on a real context, and ``user_context_scan`` must refuse its source.
"""

import inspect
import textwrap
from collections.abc import Callable
from typing import Any

import pytest

from ctx import make_context
from ownvitals.context import UserContext, require_context
from user_context_scan import scan_source


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
