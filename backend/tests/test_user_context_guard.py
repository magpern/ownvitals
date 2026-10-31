"""Guard 5 of ADR 0002 section 7, and the 'no raw user id' rule of section 1."""

from pathlib import Path

import pytest

from user_context_scan import ISSUER_MODULES, scan_source

DEPENDS = "from fastapi import Depends"
ANNOTATED = "from typing import Annotated"
SECURITY = "from fastapi import Security"
DEPENDS_AS_D = "from fastapi import Depends as D"
SECURITY_AS_S = "from fastapi import Security as S"
ANNOTATED_AS_A = "from typing import Annotated as A"


def lines(*parts: str) -> str:
    return chr(10).join(parts)


SRC = Path(__file__).resolve().parent.parent / "src"
IMPORT = "from ownvitals.context import UserContext\n"


def test_no_module_outside_the_issuers_builds_or_copies_a_user_context() -> None:
    violations = []
    paths = sorted((SRC / "ownvitals").rglob("*.py"))
    assert len(paths) > 10, "the scan must see the package"
    for path in paths:
        relative = path.relative_to(SRC).as_posix()
        if relative in ISSUER_MODULES:
            continue
        violations += [(relative, line, why) for line, why in scan_source(path.read_text("utf-8"))]
    assert violations == []


ALLOWED = [
    IMPORT + "def f(ctx: UserContext) -> UserContext: ...",
    IMPORT + "async def f(*, ctx: 'UserContext') -> None: ...",
    IMPORT + "x: list[UserContext] = []",
    "from __future__ import annotations\n"
    + IMPORT
    + "def f(a: dict[str, UserContext | None]): ...",
    "import ownvitals.context\ndef f(ctx: ownvitals.context.UserContext): ...",
    "import functools\np = functools.partial(print, 1)",  # no UserContext in this module
    "x = 'a'.replace('a', 'b')",
    lines(DEPENDS, IMPORT, "def get(): ...", "async def h(ctx: UserContext = Depends(get)): ..."),
    lines(
        ANNOTATED,
        DEPENDS,
        IMPORT,
        "def get(): ...",
        "async def h(ctx: Annotated[UserContext, Depends(get)]): ...",
    ),
    lines(DEPENDS, "def h(x: int = Depends()): ..."),
    lines(ANNOTATED, DEPENDS, IMPORT, "def h(x: Annotated[int, Depends()]): ..."),
    # A named dependency is fine, with either function and under any spelling.
    lines(SECURITY, IMPORT, "def get(): ...", "async def h(ctx: UserContext = Security(get)): ..."),
    lines(
        SECURITY,
        IMPORT,
        "def get(): ...",
        "async def h(ctx: UserContext = Security(get, scopes=['me'])): ...",
    ),
    lines(DEPENDS_AS_D, IMPORT, "def get(): ...", "async def h(ctx: UserContext = D(get)): ..."),
    lines(SECURITY_AS_S, IMPORT, "def get(): ...", "async def h(ctx: UserContext = S(get)): ..."),
    lines(
        ANNOTATED_AS_A,
        SECURITY,
        IMPORT,
        "def get(): ...",
        "async def h(ctx: A[UserContext, Security(get)]): ...",
    ),
    lines(
        ANNOTATED,
        DEPENDS,
        IMPORT,
        "def get(): ...",
        "async def h(ctx: 'Annotated[UserContext, Depends(get)]'): ...",
    ),
    lines(
        DEPENDS,
        IMPORT,
        "def get(): ...",
        "async def h(ctx: UserContext = Depends(dependency=get)): ...",
    ),
]
REFUSED = {
    "call": IMPORT + "ctx = UserContext(user_id=1, role='user')",
    "call through an alias": "from ownvitals.context import UserContext as C\nC(1, 'user')",
    "alias assignment": IMPORT + "C = UserContext\nC(1, 'user')",
    "module attribute call": "import ownvitals.context as c\nc.UserContext(1, 'user')",
    "full path call": "import ownvitals.context\nownvitals.context.UserContext(1, 'user')",
    "subclass": IMPORT + "class Mine(UserContext): ...",
    "isinstance": IMPORT + "isinstance(1, UserContext)",
    "cast": "from typing import cast\n" + IMPORT + "cast(UserContext, 1)",
    "replace": "import dataclasses\n"
    + IMPORT
    + "def f(ctx: UserContext): return dataclasses.replace(ctx, role='admin')",
    "partial": "import functools\n"
    + IMPORT
    + "def f(ctx: UserContext): return functools.partial(print, ctx)",
    "partial of the class": "import functools\n"
    + IMPORT
    + "p = functools.partial(UserContext, role='user')",
    "getattr": "import ownvitals.context as m\ngetattr(m, 'UserContext')(1, 'user')",
    "star import": "from ownvitals.context import *",
    "type(x)(...)": IMPORT + "def f(ctx: UserContext): return type(ctx)(user_id=1, role='admin')",
    "type(x)(...) in a module that never names the class": "def f(ctx): return type(ctx)(1, 'a')",
    "x.__class__(...)": "def f(ctx): return ctx.__class__(1, 'admin')",
    "object.__setattr__": "def f(ctx): object.__setattr__(ctx, 'user_id', 1)",
    "object.__new__": "def f(cls): return object.__new__(cls)",
    "replace without naming the class": "import dataclasses\n"
    "def f(ctx): return dataclasses.replace(ctx, user_id=1)",
    "replace imported without naming the class": "from dataclasses import replace\n"
    "def f(ctx): return replace(ctx, user_id=1)",
    "replace imported with an alias": "from dataclasses import replace as r\n"
    "def f(ctx): return r(ctx)",
    "issuer token import": "from ownvitals.context import issuer_token\nissuer_token()",
    "issuer token attribute": "import ownvitals.context as c\nc.issuer_token()",
    "Depends(UserContext)": lines(DEPENDS, IMPORT, "async def h(ctx=Depends(UserContext)): ..."),
    "Depends(UserContext) in Annotated": lines(
        ANNOTATED,
        DEPENDS,
        IMPORT,
        "async def h(ctx: Annotated[UserContext, Depends(UserContext)]): ...",
    ),
    "Depends(UserContext) through an alias": lines(
        "import fastapi",
        "from ownvitals.context import UserContext as C",
        "async def h(ctx=fastapi.Depends(dependency=C)): ...",
    ),
    "bare Depends() default": lines(
        DEPENDS, IMPORT, "async def h(ctx: UserContext = Depends()): ..."
    ),
    "bare Depends() keyword-only default": lines(
        DEPENDS, IMPORT, "async def h(*, ctx: UserContext = Depends()): ..."
    ),
    "bare Depends() in Annotated": lines(
        ANNOTATED, DEPENDS, IMPORT, "async def h(ctx: Annotated[UserContext, Depends()]): ..."
    ),
    "bare Depends() in typing.Annotated": lines(
        "import typing",
        DEPENDS,
        IMPORT,
        "async def h(ctx: typing.Annotated[UserContext, Depends()]): ...",
    ),
    "bare Depends() with a string annotation": lines(
        DEPENDS, IMPORT, "async def h(ctx: 'UserContext' = Depends()): ..."
    ),
    # The cases below name UserContext only inside annotations or strings, so the plain
    # reference rule does not see them: only the Depends/Security/Annotated rules refuse them.
    "bare Depends() under an import alias": lines(
        DEPENDS_AS_D, IMPORT, "async def h(ctx: UserContext = D()): ..."
    ),
    "bare Depends() under an assigned alias": lines(
        DEPENDS, IMPORT, "D = Depends", "async def h(ctx: UserContext = D()): ..."
    ),
    "bare Security() default": lines(
        SECURITY, IMPORT, "async def h(ctx: UserContext = Security()): ..."
    ),
    "bare Security() under an import alias": lines(
        SECURITY_AS_S, IMPORT, "async def h(ctx: UserContext = S()): ..."
    ),
    "Security() with only scopes": lines(
        SECURITY, IMPORT, "async def h(ctx: UserContext = Security(scopes=['me'])): ..."
    ),
    "bare Security() in Annotated": lines(
        ANNOTATED, SECURITY, IMPORT, "async def h(ctx: Annotated[UserContext, Security()]): ..."
    ),
    "Security() naming the class in a string": lines(
        SECURITY, "async def h(ctx=Security('ownvitals.context.UserContext')): ..."
    ),
    "Depends(None) default": lines(
        DEPENDS, IMPORT, "async def h(ctx: UserContext = Depends(None)): ..."
    ),
    "Depends(dependency=None) default": lines(
        DEPENDS, IMPORT, "async def h(ctx: UserContext = Depends(dependency=None)): ..."
    ),
    "Depends(None) in Annotated": lines(
        ANNOTATED, DEPENDS, IMPORT, "async def h(ctx: Annotated[UserContext, Depends(None)]): ..."
    ),
    "bare Depends() in an aliased Annotated": lines(
        ANNOTATED_AS_A, DEPENDS, IMPORT, "async def h(ctx: A[UserContext, Depends()]): ..."
    ),
    "bare Depends() in an Annotated alias assignment": lines(
        ANNOTATED,
        DEPENDS,
        IMPORT,
        "Ann = Annotated",
        "async def h(ctx: Ann[UserContext, Depends()]): ...",
    ),
    "bare Depends() in a typing_extensions Annotated": lines(
        "from typing_extensions import Annotated as X",
        DEPENDS,
        IMPORT,
        "async def h(ctx: X[UserContext, Depends()]): ...",
    ),
    "whole-string Annotated annotation": lines(
        ANNOTATED, DEPENDS, IMPORT, "async def h(ctx: 'Annotated[UserContext, Depends()]'): ..."
    ),
    "whole-string Annotated annotation with Security": lines(
        ANNOTATED, SECURITY, IMPORT, "async def h(ctx: 'Annotated[UserContext, Security()]'): ..."
    ),
    "whole-string Annotated variable annotation": lines(
        ANNOTATED, DEPENDS, IMPORT, "ctx: 'Annotated[UserContext, Depends(None)]' = None"
    ),
    "whole-string Annotated annotation with aliases": lines(
        ANNOTATED_AS_A, DEPENDS_AS_D, IMPORT, "async def h(ctx: 'A[UserContext, D()]'): ..."
    ),
    "issuer private token": "from ownvitals.context import _ISSUER\nx = _ISSUER",
    "bare Depends() through import fastapi as f": lines(
        "import fastapi as f", IMPORT, "async def h(ctx: UserContext = f.Depends()): ..."
    ),
    "bare Security() through import fastapi as f": lines(
        "import fastapi as f", IMPORT, "async def h(ctx: UserContext = f.Security()): ..."
    ),
    "bare Depends() in Annotated through import fastapi as f": lines(
        "import typing as t",
        "import fastapi as f",
        IMPORT,
        "async def h(ctx: t.Annotated[UserContext, f.Depends()]): ...",
    ),
    "Depends(UserContext) through import fastapi as f": lines(
        "import fastapi as f", IMPORT, "async def h(ctx=f.Depends(UserContext)): ..."
    ),
    "bare Depends() under an annotated alias assignment": lines(
        DEPENDS,
        "from typing import Any",
        IMPORT,
        "D: Any = Depends",
        "async def h(ctx: UserContext = D()): ...",
    ),
    "bare Depends() under a chain of annotated aliases": lines(
        DEPENDS,
        "from typing import Any",
        IMPORT,
        "D: Any = Depends",
        "E: Any = D",
        "async def h(ctx: UserContext = E()): ...",
    ),
    "Annotated alias assigned with an annotation": lines(
        ANNOTATED,
        "from typing import Any",
        DEPENDS,
        IMPORT,
        "Ann: Any = Annotated",
        "async def h(ctx: Ann[UserContext, Depends()]): ...",
    ),
    "string TypeAlias value": lines(
        "from typing import Annotated, TypeAlias",
        DEPENDS,
        IMPORT,
        "Ctx: TypeAlias = 'Annotated[UserContext, Depends()]'",
    ),
    "string TypeAlias value with typing.TypeAlias": lines(
        "import typing",
        ANNOTATED,
        DEPENDS,
        IMPORT,
        "Ctx: typing.TypeAlias = 'Annotated[UserContext, Security()]'",
    ),
    "string TypeAlias value with aliases": lines(
        "from typing import TypeAlias",
        ANNOTATED_AS_A,
        DEPENDS_AS_D,
        IMPORT,
        "Ctx: TypeAlias = 'A[UserContext, D()]'",
    ),
    "type statement": lines(
        ANNOTATED, DEPENDS, IMPORT, "type Ctx = Annotated[UserContext, Depends()]"
    ),
}


@pytest.mark.parametrize("source", ALLOWED)
def test_annotations_are_allowed(source: str) -> None:
    assert scan_source(source) == []


@pytest.mark.parametrize("source", REFUSED.values(), ids=REFUSED.keys())
def test_every_other_use_is_refused(source: str) -> None:
    assert scan_source(source) != []


def test_the_issuer_exemption_is_three_modules_not_the_auth_or_jobs_package() -> None:
    # The auth issuer (#10) and the job issuer (#9) do not exist yet; their modules are named
    # here so that the rest of both packages never gets the exemption.
    assert set(ISSUER_MODULES) == {
        "ownvitals/auth/deps.py",
        "ownvitals/jobs/issuer.py",
        "ownvitals/context.py",
    }
    route = "ownvitals/auth/routes.py"
    assert route not in ISSUER_MODULES
    assert "ownvitals/jobs/runner.py" not in ISSUER_MODULES
    forged = IMPORT + "ctx = UserContext(user_id=1, role='user')"
    assert scan_source(forged) != []  # what the scan would say about a route module that did this


def test_a_type_alias_to_a_plain_annotated_context_is_fine() -> None:
    allowed = lines(
        "from typing import Annotated, TypeAlias",
        DEPENDS,
        IMPORT,
        "def require_user(): ...",
        "Ctx: TypeAlias = 'Annotated[UserContext, Depends(require_user)]'",
        "D: TypeAlias = Depends",
    )
    assert scan_source(allowed) == []


def test_a_handler_cannot_pass_a_raw_user_id_to_a_session() -> None:
    # The only entry points take a UserContext; the runtime check is in test_isolation.py
    # (test_a_raw_user_id_is_not_a_context). Here: a handler that builds one is refused.
    handler = (
        "from fastapi import APIRouter\n" + IMPORT + "from ownvitals.db import user_session\n"
        "router = APIRouter()\n"
        "@router.get('/x/{user_id}')\n"
        "async def x(user_id: str, request):\n"
        "    ctx = UserContext(user_id=user_id, role='user')\n"
        "    async with user_session(request.app.state.session_factory, ctx): ...\n"
    )
    assert scan_source(handler) != []
