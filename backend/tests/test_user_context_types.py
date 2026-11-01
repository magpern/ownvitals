"""Guard 5's annotation rule (#89, the fourth #115 review): the allow-list type grammar of
``user_context_types``, strings in type positions judged in the module's own context, and
``eval``, ``exec`` and ``compile``.

Each forgery a review ran to an admin context runs here too, and the scan must refuse its source;
each refused case has a near miss of the same shape that stays allowed."""

import ast
import symtable
import typing
from typing import Annotated, Any, cast

import pytest

from ctx import make_context
from ownvitals.context import require_context
from user_context_scan import scan_source
from user_context_types import (  # pyright: ignore[reportPrivateUsage]
    _binding_sites,
    _rebound_in_a_scope,
    typing_names,
)


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
        'def g(x: Literal["vars(obj)"]): ...\n',
        # A call in a string: refused if parsed (a dunder in any string is refused anyway).
        TYPING + 'def g(x: Literal["vars(obj)", "user"]): ...\n',
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
    # The fifth #115 review: ForwardRef evaluates a string starting with * as (<rest>,)[0].
    "a * string on *args": (
        f'def g(*a: "*[{SETTER}]"): ...\n',
        'def g(*a: "*Ts"): ...\n',
    ),
    "a * string in a generic": (
        f'def g(x: list["*[{SETTER}]"]): ...\n',
        'def g(x: tuple["*Ts"]): ...\n',
    ),
    "a * string as the return": (
        f'def g() -> "*[{SETTER}]": ...\n',
        'def g() -> "Node": ...\n',
    ),
    "a string that doesn't parse": (
        'def g(x: "int ]"): ...\n',
        'def g(x: "int"): ...\n',
    ),
    "sys.modules['ownvitals.context'] in an annotation": (
        'import sys\ndef g(x: sys.modules["ownvitals.context"]): ...\n',
        'import sys\ndef g(x: dict["ownvitals.db", int]): ...\n',
    ),
    "an aliased TypeAlias": (
        f'from typing import TypeAlias as TA\nS: TA = "{SETTER}"\n',
        'from typing import TypeAlias as TA\nS: TA = "list[int]"\n',
    ),
    "issuer_token as an attribute": (
        "import ownvitals.context\ndef g(x: ownvitals.context.issuer_token): ...\n",
        "import ownvitals.context\ndef g(x: ownvitals.context.UserContext): ...\n",
    ),
    "issuer_token as an attribute, in a string": (
        'import ownvitals.context\ndef g(x: "ownvitals.context.issuer_token"): ...\n',
        'import ownvitals.context\ndef g(x: "ownvitals.context.UserContext"): ...\n',
    ),
    "a concatenation in a type": (
        'def g(x: list["getattr(object, " + "\'__setattr__\')"]): ...\n',
        "def g(x: int | None): ...\n",
    ),
    # Each half parses and passes alone; the concatenation is a ForwardRef for "make()".
    "a concatenation of parsing halves": (
        'def g(x: list["make" + "()"]): ...\n',
        'def g(x: list["make"] | None): ...\n',
    ),
    "a call in a *args type": ("def g(*a: *make()): ...\n", "def g(*a: *Ts): ...\n"),
    "a dunder name": (
        'def g(x: list["__builtins__"]): ...\n',
        'class _Policy: ...\n_T = 1\ndef g(x: list["_Policy"], y: _T) -> _T: ...\n',
    ),
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
    "a non-typing import aliased as typing's name": (
        "from typing import Annotated\nimport mylib as Annotated\n"
        "def g(x: Annotated[int, f()]): ...\n",
        "from typing import Annotated\nimport mylib as Other\ndef g(x: Annotated[int, f()]): ...\n",
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


# The sixth #115 review (#89): Literal or Annotated rebound in some scope is not typing's, and
# a subscript named so that doesn't resolve is refused (a misread exemption fails closed).
L = "from typing import Literal\n"
BINDINGS: dict[str, tuple[str, str]] = {
    "a parameter named Literal": (
        L + f'def make(Literal):\n    def g(x: Literal["{SETTER}"]): ...\n',
        L + 'def make(kind):\n    def g(x: Literal["user"]): ...\n',
    ),
    "a parameter named Annotated": (
        "from typing import Annotated\n"
        f'def make(Annotated):\n    def g(x: Annotated[int, "{SETTER}"]): ...\n',
        'from typing import Annotated\ndef make(kind):\n    def g(x: Annotated[int, "doc"]): ...\n',
    ),
    "a match capture": (
        L + f'match list:\n    case Literal:\n        def g(x: Literal["{SETTER}"]): ...\n',
        'import typing\nmatch v:\n    case other:\n        def g(x: typing.Literal["user"]): ...\n',
    ),
    "a parameter named like the typing alias": (
        f'import typing as t\ndef make(t):\n    def g(x: t.Literal["{SETTER}"]): ...\n',
        'import typing as t\ndef make(u):\n    def g(x: t.Literal["user"]): ...\n',
    ),
    "except ... as": (
        L + 'try:\n    pass\nexcept Exception as Literal:\n    pass\ndef g(x: Literal["a"]): ...\n',
        L + 'try:\n    pass\nexcept Exception as err:\n    pass\ndef g(x: Literal["a"]): ...\n',
    ),
    "a comprehension variable": (
        L + 'v = [0 for Literal in [list]]\ndef g(x: Literal["a"]): ...\n',
        L + 'v = [0 for item in [list]]\ndef g(x: Literal["a"]): ...\n',
    ),
    "the walrus": (
        L + 'if (Literal := list):\n    pass\ndef g(x: Literal["a"]): ...\n',
        L + 'if (kind := list):\n    pass\ndef g(x: Literal["a"]): ...\n',
    ),
    "global": (
        L + 'def h():\n    global Literal\n    Literal = list\ndef g(x: Literal["a"]): ...\n',
        L + 'def h():\n    global other\n    other = list\ndef g(x: Literal["a"]): ...\n',
    ),
    "a PEP 695 type parameter": (
        L + 'def f[Literal](x: Literal["a"]): ...\n',
        L + 'def f[T](x: T, y: Literal["a"]): ...\n',
    ),
    "a lambda parameter": (
        L + 'f = lambda Literal: 0\ndef g(x: Literal["a"]): ...\n',
        L + 'f = lambda kind: 0\ndef g(x: Literal["a"]): ...\n',
    ),
    "an alias from another module": (
        'from mylib import Literal as Lit\ndef g(x: Lit["a"]): ...\n',
        'from typing import Literal as Lit\ndef g(x: Lit["a"]): ...\n',
    ),
    # Mutations b, c and e of the review: the type element of Annotated is a type (spliced and
    # descended into), and a real Literal's arguments still may not name the token.
    "Annotated nested in Annotated's type": (
        TYPING + f"def g(x: Annotated[Annotated[int, {SETTER}], 1]): ...\n",
        TYPING + "def g(x: Annotated[Annotated[int, Depends(get)], 1]): ...\n",
    ),
    "a string as Annotated's type": (
        TYPING + f'def g(x: Annotated["{SETTER}", 1]): ...\n',
        TYPING + 'def g(x: Annotated["int", 1]): ...\n',
    ),
    # A star import binds names neither symtable nor the AST sees (a synthetic module exporting
    # ``Literal = list`` or ``t``), so after one no spelling of typing's names is trusted.
    "a star import after typing's Literal": (
        L + 'from synthetic_lib import *\ndef g(x: Literal["a"]): ...\n',
        L + 'from typing import *\ndef g(x: Literal["a"]): ...\n',
    ),
    "a star import after the typing alias": (
        'import typing as t\nfrom synthetic_lib import *\ndef g(x: t.Literal["a"]): ...\n',
        'import typing as t\nfrom typing_extensions import *\ndef g(x: t.Literal["a"]): ...\n',
    ),
    "the token's name in typing's Literal": (
        TYPING + "x: Literal['_ISSUER'] = None\n",
        TYPING + "x: Literal['_user'] = None\n",
    ),
}


@pytest.mark.parametrize(("refused", "_"), BINDINGS.values(), ids=BINDINGS.keys())
def test_a_rebound_or_unresolved_literal_or_annotated_is_refused(refused: str, _: str) -> None:
    assert scan_source(refused) != []


@pytest.mark.parametrize(("_", "near"), BINDINGS.values(), ids=BINDINGS.keys())
def test_typing_literal_and_annotated_stay_allowed(_: str, near: str) -> None:
    assert scan_source(near) == []


@pytest.mark.parametrize(
    ("star", "trusted"),
    [
        ("from synthetic_lib import *\n", set[str]()),
        ("from typing_foo import *\n", set[str]()),  # named like typing, but not typing
        ("from .typing import *\n", set[str]()),  # the scan refuses it anyway (a relative star)
        ("from typing import *\n", {"Literal"}),
        ("from typing_extensions import *\n", {"Literal"}),
    ],
)
def test_a_star_import_from_elsewhere_binds_every_name(star: str, trusted: set[str]) -> None:
    source = "from typing import Literal\n" + star
    table = symtable.symtable(source, "<test>", "exec")
    assert typing_names(ast.parse(source), table, "Literal") == trusted


def test_the_typing_alias_imported_again_from_another_module_is_not_typing() -> None:
    assert scan_source('import typing as t\nimport mylib as t\ndef g(x: t.Literal["a"]): ...\n')
    assert not scan_source('import typing as t\nimport mylib as u\ndef g(x: t.Literal["a"]): ...\n')


# Both views of the bindings are checked on their own: the compiler's (symtable) and the AST's.
REBINDINGS = {
    "a parameter": "def f(Literal): ...\n",
    "a lambda parameter": "f = lambda Literal: 0\n",
    "except ... as": "try:\n    pass\nexcept Exception as Literal:\n    pass\n",
    "a match capture": "match v:\n    case Literal:\n        pass\n",
    "the walrus": "if (Literal := 1):\n    pass\n",
    "global": "def h():\n    global Literal\n    Literal = 1\n",
    "a PEP 695 parameter": "def f[Literal](): ...\n",
    "del": "del Literal\n",
    "a def": "def Literal(): ...\n",
    "a class": "class Literal: ...\n",
    "a nested def": "def f():\n    def Literal(): ...\n",
}


@pytest.mark.parametrize("rebinding", REBINDINGS.values(), ids=REBINDINGS.keys())
def test_symtable_sees_each_rebinding(rebinding: str) -> None:
    source = "from typing import Literal\n" + rebinding
    table = symtable.symtable(source, "<test>", "exec")
    assert _rebound_in_a_scope(table, "Literal")
    assert not _rebound_in_a_scope(table, "other")


@pytest.mark.parametrize(
    "rebinding",
    [*REBINDINGS.values(), "v = [0 for Literal in []]\n", "def h():\n    global Literal\n"],
    ids=[*REBINDINGS.keys(), "a comprehension variable", "a global declaration alone"],
)
def test_the_ast_sees_each_rebinding(rebinding: str) -> None:
    sites = _binding_sites(ast.parse("from typing import Literal\n" + rebinding))
    assert len(sites["Literal"]) >= 2  # the import and the rebinding


class _Symbol:
    """A symbol that is only a namespace (``def``/``class``): CPython also marks those
    assigned, so this pins that ``_rebound_in_a_scope`` asks for namespaces on its own."""

    def is_parameter(self) -> bool:
        return False

    def is_assigned(self) -> bool:
        return False

    def is_namespace(self) -> bool:
        return True


class _Table:
    def get_children(self) -> list["_Table"]:
        return []

    def lookup(self, name: str) -> _Symbol:
        if name != "Literal":
            raise KeyError(name)
        return _Symbol()


def test_a_namespace_alone_is_a_rebinding() -> None:
    table = cast(symtable.SymbolTable, _Table())
    assert _rebound_in_a_scope(table, "Literal")
    assert not _rebound_in_a_scope(table, "other")


def test_an_import_alone_is_no_rebinding() -> None:
    source = "from typing import Literal\nimport typing as t\n"
    table = symtable.symtable(source, "<test>", "exec")
    assert not _rebound_in_a_scope(table, "Literal")
    assert not _rebound_in_a_scope(table, "t")
    assert len(_binding_sites(ast.parse(source))["Literal"]) == 1
