"""D1 of the FAIL on 95ee6f6 (#123): every string ``typing`` evaluates in an annotation, a
``TypeVar`` or a ``ForwardRef`` holds only names, judged as code (``egress_forward``). Whole-string
annotations are in ``test_egress_importers.py``.
"""

import pytest

from egress_scan import scan_source

M = "ownvitals.web.planted"
RUNS = "a string annotation holds more than names"
BUILDS = "an annotation builds a string"
NATIVE = "calls native code the egress hook cannot see"
LOADS = "loads a module by a name the guard cannot judge"
IMPORTER = "in an annotation: an importer"
CALL = "__import__('ctypes').CDLL"

REFUSED = [
    (f'def f(x: list["{CALL}"]) -> None: ...', RUNS),
    (
        "from typing import Optional\nclass M:\n"
        "    x: Optional[\"__import__('_posixsubprocess').fork_exec\"] = None",
        RUNS,
    ),
    (f'x: dict[str, "{CALL}"] = {{}}', RUNS),
    ("def f() -> list[\"__import__('ctypes')\"]: ...", RUNS),
    ("from typing import Annotated\nx: Annotated[\"__import__('ctypes')\", 1]", RUNS),
    # The reviewer's live case: pydantic evaluates it when it builds the class.
    (
        "from pydantic import BaseModel\nclass M(BaseModel):\n"
        "    x: list[\"__import__('ctypes') and int\"] = []",
        RUNS,
    ),
    ("x: tuple[int, list[\"__import__('json')\"]]", RUNS),
    ("x: list[int] | \"__import__('json')\"", RUNS),
    (f'def f(x: f"{CALL}") -> None: ...', BUILDS),
    (f'x: "{CALL}" + ""', BUILDS),
    ("x: list[f'{y}']", BUILDS),
    ("x: 'a' * 2", BUILDS),
    (f'from typing import TypeVar\nT = TypeVar("T", bound="{CALL}")', RUNS),
    ("import typing\nT = typing.TypeVar('T', \"__import__('ctypes')\", int)", RUNS),
    ("from typing import TypeVar\nT = TypeVar('T', bound=f'x')", BUILDS),
    ("from typing import ForwardRef\nForwardRef(\"__import__('ctypes')\")", RUNS),
    ('x: list["ctypes.CDLL"]', NATIVE),
    ('from typing import TypeVar\nT = TypeVar("T", bound="ctypes.CDLL")', NATIVE),
    # NB2: the importer names inside a forward reference.
    ("x: 'Literal[__import__]'", IMPORTER),
    ("x: '__builtins__'", IMPORTER),
    ("x: list['__import__']", IMPORTER),
    ("import importlib\nx: 'importlib.import_module'", IMPORTER),
    # Q3: a name in a forward reference resolves through the run-time imports first.
    ('import importlib as il\nx: "il.machinery.PathFinder"', LOADS),
]
NEAR_MISSES = [
    'x: list["Node"]',
    'from typing import Optional\nx: Optional["Node"] = None',
    'from typing import Literal\nx: Literal["a b"]',
    'from typing import Literal\nx: list[Literal["a b", "(c)"]]',
    'import typing\nx: typing.Literal["a b", "c-d"]',  # values, never evaluated
    'from typing import Annotated\nx: Annotated[int, "any metadata, (here)"]',
    "from typing import Annotated\nfrom pydantic import Field\n"
    'x: Annotated[str, Field(description="free text, (here)")]',
    'from typing import TypeVar\nT = TypeVar("T", bound="Node")',
    "from typing import TypeVar\nT = TypeVar('T', int, str)",
    "from typing import ForwardRef\nForwardRef('Node')",
    "x: int | None = None",
]


@pytest.mark.parametrize(("source", "why"), REFUSED)
def test_a_string_typing_evaluates_holds_only_names(source: str, why: str) -> None:
    found = scan_source(source, M)
    assert any(why in w for _, w in found), found


@pytest.mark.parametrize("source", NEAR_MISSES)
def test_the_near_misses_pass(source: str) -> None:
    assert scan_source(source, M) == []
