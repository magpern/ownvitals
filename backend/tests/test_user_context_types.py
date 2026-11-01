"""Guard 5's annotation rule (#89, the fourth #115 review): the allow-list type grammar of
``user_context_types``, strings in type positions judged in the module's own context, and
``eval``, ``exec`` and ``compile``.

Each forgery a review ran to an admin context runs here too, and the scan must refuse its source;
each refused case has a near miss of the same shape that stays allowed."""

import typing
from typing import Annotated, Any

import pytest

from ctx import make_context
from ownvitals.context import require_context
from user_context_scan import scan_source


# The review's two forgeries: a string nested in a generic is evaluated by get_type_hints.
def nested_string_metadata(ctx: Any) -> Any:
    def g(x: list["Annotated[int, getattr(object, '__setattr__')]"]) -> None: ...  # pyright: ignore  # noqa: B009

    hint = typing.get_type_hints(g, include_extras=True)["x"]
    hint.__args__[0].__metadata__[0](ctx, "role", "admin")
    return require_context(ctx)


def nested_string_type(ctx: Any) -> Any:
    def g(x: list["getattr(object, '__setattr__')"]) -> None: ...  # pyright: ignore  # noqa: B009

    typing.get_type_hints(g)["x"].__args__[0](ctx, "role", "admin")
    return require_context(ctx)


@pytest.mark.parametrize("forgery", [nested_string_metadata, nested_string_type])
def test_a_nested_string_annotation_runs_as_code(forgery: Any) -> None:
    assert forgery(make_context(role="user")).role == "admin"


SETTER = "getattr(object, '__setattr__')"
# The setter inside a string inside a string: list["list['getattr(object, \"__setattr__\")']"].
INNER_SETTER = repr('getattr(object, "__setattr__")')
TYPING = "from typing import Annotated, Literal, TypeAlias\nfrom fastapi import Depends\n"


def nested(depth: int) -> str:
    """An annotation that is a string of a string ... of ``int``, ``depth`` levels deep."""
    text = "int"
    for _ in range(depth):
        text = repr(text)
    return f"x: {text} = 1\n"


ROUTES: dict[str, tuple[str, str]] = {
    "list[Annotated[...] in a string]": (
        TYPING + f'def g(x: list["Annotated[int, {SETTER}]"]): ...\n',
        TYPING + 'def g(x: list["Annotated[int, Depends(get)]"]): ...\n',
    ),
    "list[a call in a string]": (
        TYPING + f'def g(x: list["{SETTER}"]): ...\n',
        TYPING + 'from ownvitals.context import UserContext\ndef g(x: list["UserContext"]): ...\n',
    ),
    "a TypeAlias string": (
        TYPING + f'S: TypeAlias = "{SETTER}"\n',
        TYPING + 'S: TypeAlias = "list[int] | None"\n',
    ),
    "vars(ownvitals.context) in a string": (
        TYPING + "import ownvitals.context\n"
        'def g(x: "Annotated[int, vars(ownvitals.context)]"): ...\n',
        TYPING + "import ownvitals.context\n"
        'def g(x: "Annotated[ownvitals.context.UserContext, Depends(get)]"): ...\n',
    ),
    "an aliased UserContext in a string": (
        TYPING + "from ownvitals.context import UserContext as U\n"
        "def g(x: \"Annotated[int, U(1, 'admin')]\"): ...\n",
        TYPING + "from ownvitals.context import UserContext as U\n"
        'def g(x: "Annotated[U, Depends(get)]"): ...\n',
    ),
    "dataclasses.replace in a string": (
        TYPING + "from dataclasses import replace\n"
        "def g(ctx, x: \"Annotated[int, replace(ctx, role='admin')]\"): ...\n",
        TYPING + 'from pydantic import Field\ndef g(x: "Annotated[int, Field(ge=1, le=9)]"): ...\n',
    ),
    "a string nested in a string": (
        TYPING + f"def g(x: list[{INNER_SETTER!r}]): ...\n",
        TYPING + f"def g(x: list[{repr('int')!r}]): ...\n",
    ),
    "strings nested too deep": (nested(9), nested(3)),
    "Literal not from typing is parsed": (
        f'def g(x: Literal["{SETTER}"]): ...\n',
        TYPING + f'def g(x: Literal["{SETTER}", "user"]): ...\n',
    ),
    "a non-constant in Literal": (
        TYPING + "def g(x: Literal[make()]): ...\n",
        TYPING + "def g(x: Literal['user', 'admin', Color.RED, None]): ...\n",
    ),
    "forward references": (
        TYPING + 'def g(x: "Node.__dict__") -> "list[Node]": ...\n',
        TYPING + 'def g(x: "Node") -> "list[Node] | None": ...\nv: "dict[str, int]" = {}\n',
    ),
    "a PEP 695 alias": (
        f'type S = list["{SETTER}"]\n',
        'type S = list["int"] | None\n',
    ),
    "a PEP 695 bound": (
        f'def g[T: "{SETTER}"](x: T): ...\n',
        'def g[T: "int | str"](x: T) -> T: ...\n',
    ),
    "a call on an attribute in a type": (
        "def g(x: make().Type): ...\n",
        "def g(x: module.Type): ...\n",
    ),
    "a comprehension in a type": (
        "def g(x: [t for t in ts]): ...\n",
        "def g(x: Callable[[int, str], None]): ...\n",
    ),
    # A string in Annotated's metadata is a value of that code, not a type: it is never parsed.
    "a string in metadata": (
        TYPING + "def g(x: Annotated[int, Field(description=eval('1'))]): ...\n",
        TYPING + "def g(x: Annotated[int, Field(description=\"eval('1')\")]): ...\n",
    ),
    "a walrus in a type": ("def g(x: (y := int)): ...\n", "def g(x: tuple[*Ts]): ...\n"),
}
# The three rebinding mutations the review saw survive: typing's Annotated rebound by a def or
# class, by another module's import, and its module alias rebound. Metadata is code only for
# typing's Annotated; for anything else the call is in a type position.
REBOUND: dict[str, tuple[str, str]] = {
    "def": (
        "from typing import Annotated\ndef Annotated(): ...\ndef g(x: Annotated[int, f()]): ...\n",
        "from typing import Annotated\ndef g(x: Annotated[int, f()]): ...\n",
    ),
    "class": (
        "from typing import Annotated\nclass Annotated: ...\ndef g(x: Annotated[int, f()]): ...\n",
        "from typing import Annotated\nclass Other: ...\ndef g(x: Annotated[int, f()]): ...\n",
    ),
    "another module's import": (
        "from typing import Annotated\nfrom mylib import Annotated\n"
        "def g(x: Annotated[int, f()]): ...\n",
        "from typing import Annotated\nfrom mylib import Other\n"
        "def g(x: Annotated[int, f()]): ...\n",
    ),
    "the module alias": (
        "import typing as t\nt = dict\ndef g(x: t.Annotated[int, f()]): ...\n",
        "import typing as t\nu = dict\ndef g(x: t.Annotated[int, f()]): ...\n",
    ),
}
CODE_BUILTINS: dict[str, tuple[str, str]] = {
    "eval": ("x = eval('1')\n", "import ast\nx = ast.literal_eval('1')\n"),
    "exec": ("exec(code)\n", "cursor.execute(code)\n"),
    "compile": ("compile(s, 'f', 'exec')\n", "import re\nre.compile('x')\n"),
    "eval as a value": ("run = eval\n", "run = evaluate\n"),
    "builtins.eval": (
        "import builtins\nbuiltins.eval('1')\n",
        "import builtins\nbuiltins.len('1')\n",
    ),
    "an aliased builtins": ("import builtins as b\nb.exec(c)\n", "import other as b\nb.exec(c)\n"),
    "__builtins__": ("__builtins__.compile(s, 'f', 'exec')\n", "obj.compile(s)\n"),
    "imported from builtins": ("from builtins import eval as e\n", "from builtins import len\n"),
}
CASES = {
    **{f"route: {k}": v for k, v in ROUTES.items()},
    **{f"rebound: {k}": v for k, v in REBOUND.items()},
    **{f"code builtin: {k}": v for k, v in CODE_BUILTINS.items()},
}


@pytest.mark.parametrize(("refused", "_"), CASES.values(), ids=CASES.keys())
def test_the_scan_refuses_each_case(refused: str, _: str) -> None:
    assert scan_source(refused) != []


@pytest.mark.parametrize(("_", "near"), CASES.values(), ids=CASES.keys())
def test_each_near_miss_stays_allowed(_: str, near: str) -> None:
    assert scan_source(near) == []


def test_the_review_forgeries_are_refused_as_written() -> None:
    import inspect
    import textwrap

    for forgery in (nested_string_metadata, nested_string_type):
        assert scan_source(textwrap.dedent(inspect.getsource(forgery))) != []
