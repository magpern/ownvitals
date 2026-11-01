"""Guard 5 of ADR 0002 section 7, continued (#89, the #115 reviews): the issuer token's routes
through the module's namespace, annotations and ``Annotated``'s metadata, and star imports.

Each forgery that a review ran to an admin context is run here too, and the scan must refuse
its source; each refused spelling has a near miss of the same shape that stays allowed. Guard 5's
threat model (``docs/dev/isolation.md``) bounds what the scan is asked to see.
"""

import inspect
import textwrap
from typing import Annotated, Any

import pytest

from ctx import make_context
from ownvitals.context import issuer_token, require_context
from test_user_context_writers import body
from user_context_scan import scan_source


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
        NEAR_MISS,  # ``__dict__`` is allow-listed only in ``log.py``
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
        "from typing import Annotated\nv: Annotated[K, Field(alias='a')] = 1\n",
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
        "from typing import Literal\nx: Literal['user', 'admin'] = 'user'\n",
    ),
    "the token's name in Literal": (
        "x: Literal['_ISSUER'] = None\n",
        "from typing import Literal\nx: Literal['_user'] = None\n",
    ),
    "issuer= in Annotated's metadata": (
        "from typing import Annotated\ndef f(x: Annotated[int, Field(issuer=t)]): ...\n",
        "from typing import Annotated\ndef f(x: Annotated[int, Field(alias='a')]): ...\n",
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
    "__kwdefaults__": (body("f.__kwdefaults__['issuer'] = t"), body("return f.kwdefaults")),
    "__defaults__": (body("f.__defaults__ = (t,)"), body("return f.defaults")),
}


@pytest.mark.parametrize(("route", "_"), ANNOTATION_ROUTES.values(), ids=ANNOTATION_ROUTES.keys())
def test_the_scan_refuses_the_annotation_and_star_import_routes(route: str, _: str) -> None:
    assert scan_source(route) != []


@pytest.mark.parametrize(("_", "near"), ANNOTATION_ROUTES.values(), ids=ANNOTATION_ROUTES.keys())
def test_the_annotation_and_star_import_near_misses_stay_allowed(_: str, near: str) -> None:
    assert scan_source(near) == []


# The third #115 review (#89): ``Annotated``'s metadata is code, evaluated with the annotation,
# so every rule for code applies to it; only ``typing``'s ``Annotated`` has metadata.
def metadata_writes_a_field(ctx: Any) -> Any:
    # The review's exact spelling: getattr with a literal name (refused by the scan elsewhere).
    def g(x: Annotated[int, getattr(object, "__setattr__")(ctx, "role", "admin")]) -> None: ...  # noqa: B009

    return require_context(ctx)


def test_annotated_metadata_runs_as_code() -> None:
    forged = metadata_writes_a_field(make_context(role="user"))
    assert forged.role == "admin"


ANNOTATED = "from typing import Annotated\nfrom fastapi import Depends, Header\n"
METADATA_ROUTES: dict[str, tuple[str, str]] = {
    "the review's forgery": (
        textwrap.dedent(inspect.getsource(metadata_writes_a_field)),
        ANNOTATED + "def g(x: Annotated[int, Depends(get)]) -> None: ...\n",
    ),
    "the setter taken in metadata": (
        ANNOTATED + "def g(x: Annotated[int, getattr(object, '__setattr__')]): ...\n",
        ANNOTATED + "def g(x: Annotated[str, Header()]): ...\n",
    ),
    "vars(ownvitals.context)": (
        ANNOTATED + "import ownvitals.context\n"
        "def g(x: Annotated[int, vars(ownvitals.context)]): ...\n",
        ANNOTATED + "import ownvitals.context\n"
        "def g(x: Annotated[ownvitals.context.UserContext, Depends(get)]): ...\n",
    ),
    "UserContext(...) in metadata": (
        ANNOTATED + "from ownvitals.context import UserContext\n"
        "def g(x: Annotated[int, UserContext(1, 'admin')]): ...\n",
        ANNOTATED + "from ownvitals.context import UserContext\n"
        "def g(x: Annotated[UserContext, Depends(get)]): ...\n",
    ),
    "a whole-string annotation's metadata": (
        ANNOTATED + "def g(x: \"Annotated[int, getattr(object, '__setattr__')]\"): ...\n",
        ANNOTATED + "def g(x: 'Annotated[int, Depends(get)]'): ...\n",
    ),
    "a typing alias": (
        "import typing as t\ndef g(x: t.Annotated[int, vars(ownvitals.context)]): ...\n"
        "import ownvitals.context\n",
        "import typing as t\nfrom pydantic import Field\n"
        "def g(x: t.Annotated[int, Field(ge=1, le=9)]): ...\n",
    ),
    # Only typing's Annotated has metadata; anything else named so is refused (#89, sixth
    # review): a misread exemption fails closed.
    "Annotated = dict": (
        "Annotated = dict\ndef g(x: Annotated[int, make()]): ...\n",
        "Mapping = dict\ndef g(x: Mapping[int, str]): ...\n",
    ),
    "typing's Annotated rebound to dict": (
        "from typing import Annotated\nAnnotated = dict\ndef g(x: Annotated[int, make()]): ...\n",
        "from typing import Annotated\ndef g(x: Annotated[int, make()]): ...\n",
    ),
    "Annotated not imported": (
        "def g(x: Annotated[int, make()]): ...\n",
        "def g(x: Mapping[int, str]): ...\n",
    ),
    "Annotated from another module": (
        "from mylib import Annotated\ndef g(x: Annotated[int, make()]): ...\n",
        "from typing_extensions import Annotated\ndef g(x: Annotated[int, make()]): ...\n",
    ),
    # The type position (elts[0]) stays an annotation.
    "a call in the type position": (
        ANNOTATED + "def g(x: Annotated[make(), Depends(get)]): ...\n",
        ANNOTATED + "def g(x: Annotated[list[int], Depends(get)]): ...\n",
    ),
}


@pytest.mark.parametrize(("route", "_"), METADATA_ROUTES.values(), ids=METADATA_ROUTES.keys())
def test_the_scan_refuses_the_metadata_routes(route: str, _: str) -> None:
    assert scan_source(route) != []


@pytest.mark.parametrize(("_", "near"), METADATA_ROUTES.values(), ids=METADATA_ROUTES.keys())
def test_the_metadata_near_misses_stay_allowed(_: str, near: str) -> None:
    assert scan_source(near) == []
