"""Issue #122: an ``if TYPE_CHECKING:`` block is skipped only when its flag is ``typing``'s (or
``typing_extensions``') and never rebound; otherwise it is scanned as the code it is. Any write
to ``TYPE_CHECKING`` is refused (``egress_typeonly``).

Each planted block imports ``ctypes``: when the block is scanned the native rule refuses it.
"""

import pytest

from egress_scan import scan_source

M = "ownvitals.web.planted"
NATIVE = "calls native code the egress hook cannot see"
ASSIGNS = "assigns TYPE_CHECKING"
BLOCK = "if TYPE_CHECKING:\n    import ctypes\n"
DOTTED = "if typing.TYPE_CHECKING:\n    import ctypes\n"
FROM = "from typing import TYPE_CHECKING\n"

# The flag is written: the write is refused and the block is scanned.
WRITTEN = [
    # The case found in #123: the annotation is evaluated when ``f`` is defined.
    FROM + "TYPE_CHECKING = True\n" + BLOCK + "def f(x: ctypes.CDLL(None)) -> None: ...",
    FROM + "TYPE_CHECKING: bool = True\n" + BLOCK,
    FROM + "TYPE_CHECKING |= True\n" + BLOCK,
    FROM + "del TYPE_CHECKING\nTYPE_CHECKING = 1\n" + BLOCK,
    FROM + "(TYPE_CHECKING := True)\n" + BLOCK,
    FROM + "for TYPE_CHECKING in [True]:\n    pass\n" + BLOCK,
    FROM + "with open('x') as TYPE_CHECKING:\n    pass\n" + BLOCK,
    FROM + "try:\n    pass\nexcept OSError as TYPE_CHECKING:\n    pass\n" + BLOCK,
    FROM + "def set_flag():\n    global TYPE_CHECKING\n    TYPE_CHECKING = True\n" + BLOCK,
    FROM + "def f(TYPE_CHECKING=True):\n    return TYPE_CHECKING\n" + BLOCK,
    FROM + "class TYPE_CHECKING:\n    pass\n" + BLOCK,
    FROM + "def TYPE_CHECKING():\n    pass\n" + BLOCK,
    FROM + "match 1:\n    case TYPE_CHECKING:\n        pass\n" + BLOCK,
    "import typing\ntyping.TYPE_CHECKING = True\n" + DOTTED,
    "import typing\nsetattr(typing, 'TYPE_CHECKING', True)\n" + DOTTED,
    "import typing as t\nt.TYPE_CHECKING = True\nif t.TYPE_CHECKING:\n    import ctypes\n",
]
# The flag is not ``typing``'s, or its module name is rebound: the block is scanned.
NOT_TYPING = [
    "TYPE_CHECKING = True\n" + BLOCK,  # also a write
    "import os as typing\n" + DOTTED,
    "import typing\nimport os as typing\n" + DOTTED,
    "import typing\ntyping = object()\n" + DOTTED,
    "from .flags import TYPE_CHECKING\n" + BLOCK,
    FROM + "from ownvitals.flags import TYPE_CHECKING\n" + BLOCK,
    FROM + "def f():\n    from os import TYPE_CHECKING\n" + BLOCK,
    "import typing\ndef f(typing):\n    pass\n" + DOTTED,
    BLOCK,  # never imported
]
NEAR_MISSES = [
    FROM + BLOCK + "def f(x: 'ctypes.c_int') -> None: ...",
    FROM + BLOCK + "def f(x: ctypes.c_int) -> None: ...",
    "import typing\n" + DOTTED + "x: 'ctypes.c_int | None' = None",
    "import typing as t\nif t.TYPE_CHECKING:\n    import ctypes\n",
    "from typing import TYPE_CHECKING as TC\nif TC:\n    import ctypes\n",
    "from typing_extensions import TYPE_CHECKING\n" + BLOCK,
    "import typing_extensions\nif typing_extensions.TYPE_CHECKING:\n    import ctypes\n",
    FROM + "import typing\n" + BLOCK + DOTTED,
    FROM + BLOCK + "type_checking = True\nTYPE_CHECKING_MODE = 1\n",  # look-alike names
    FROM + BLOCK + "def f() -> bool:\n    return TYPE_CHECKING\n",  # read, not written
]


@pytest.mark.parametrize("source", WRITTEN)
def test_a_write_to_the_flag_is_refused_and_the_block_scanned(source: str) -> None:
    found = [why for _, why in scan_source(source, M)]
    assert any(ASSIGNS in why for why in found), found
    assert any(NATIVE in why for why in found), found


@pytest.mark.parametrize("source", NOT_TYPING)
def test_a_flag_that_is_not_typings_runs_its_block(source: str) -> None:
    assert any(NATIVE in why for _, why in scan_source(source, M))


@pytest.mark.parametrize("source", NEAR_MISSES)
def test_a_type_only_import_of_typings_flag_passes(source: str) -> None:
    assert scan_source(source, M) == []
