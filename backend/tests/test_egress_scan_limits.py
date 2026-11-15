"""#395: the egress scan is bounded. The chain rule of #284 judges every prefix of a chain at
every node of it, so an unbounded ``a.b.c...`` would make one module's scan cost grow with the
cube of its length. Past ``MAX_CHAIN_PARTS`` parts (an attribute chain, an imported name, a
dotted string, or a name ``classify`` is handed) or ``MAX_SOURCE_BYTES`` the module is refused,
never truncated, so the guard fails closed; ``src/`` stays well inside both."""

import ast
import time
from pathlib import Path

import pytest

from egress_scan import (
    MAX_CHAIN_PARTS,
    MAX_SOURCE_BYTES,
    classify,
    importable,
    sources,
    visits,
)
from egress_scan import scan_source as scan

SRC = Path(__file__).resolve().parent.parent / "src"
RUNNER = "ownvitals.db.install_functions"  # the one module psycopg is classified for
LIMIT = "the scan's limit"


def _chain(head: str, parts: int) -> str:
    """``head`` and ``parts - 1`` further steps."""
    return ".".join([head, *(f"a{i}" for i in range(parts - 1))])


def test_src_stays_well_inside_the_limits() -> None:
    deepest = 0
    for module, source in sources(SRC):
        assert len(source.encode()) <= MAX_SOURCE_BYTES // 4, module
        for node in ast.walk(ast.parse(source)):
            parts, inner = 1, node
            while isinstance(inner, ast.Attribute):
                parts, inner = parts + 1, inner.value
            deepest = max(deepest, parts)
    assert deepest <= MAX_CHAIN_PARTS // 2


@pytest.mark.parametrize("parts", [MAX_CHAIN_PARTS - 1, MAX_CHAIN_PARTS])
def test_a_chain_at_the_limit_is_still_judged(parts: int) -> None:
    """At the limit the ordinary rules apply: a step past an allowed name is refused as such."""
    found = scan("import psycopg\n" + _chain("psycopg.connect", parts - 1), RUNNER)
    assert found
    assert all(LIMIT not in why for _, why in found)
    assert any("past an allowed name of psycopg" in why for _, why in found)


@pytest.mark.parametrize(
    "source",
    [
        _chain("x", MAX_CHAIN_PARTS + 1),  # an attribute chain on any name
        "import psycopg\n" + _chain("psycopg.connect", MAX_CHAIN_PARTS),
        f"import {_chain('os', MAX_CHAIN_PARTS + 1)}",
        f"from {_chain('os', MAX_CHAIN_PARTS + 1)} import y",
        f"x = '{_chain('socket', MAX_CHAIN_PARTS + 1)}'",  # a dotted string
        f"f().{_chain('b', MAX_CHAIN_PARTS)}",  # a call at the base counts as a part
    ],
)
def test_over_the_limit_the_module_is_refused_whole(source: str) -> None:
    found = scan(source, RUNNER)
    assert len(found) == 1
    assert LIMIT in found[0][1]


def test_a_module_over_the_size_limit_is_refused_before_it_is_parsed() -> None:
    source = "x = 1\n" + "#" * MAX_SOURCE_BYTES
    assert scan(source, RUNNER) == [(1, f"the module is over {MAX_SOURCE_BYTES} bytes, {LIMIT}")]
    assert scan("x = 1\n", RUNNER) == []


def test_classify_refuses_a_longer_name_it_is_handed() -> None:
    """An alias head with its chain can pass the per-node bound and still be too long."""
    name = _chain("socket", MAX_CHAIN_PARTS + 1)
    found = classify(name, RUNNER, "uses")
    assert len(found) == 1
    assert LIMIT in found[0]
    assert visits(name) == []
    assert not importable(name)
    assert classify(_chain("socket", MAX_CHAIN_PARTS), RUNNER, "uses") != []
    assert all(LIMIT not in w for w in classify(_chain("socket", MAX_CHAIN_PARTS), RUNNER, "uses"))


def test_a_hostile_chain_costs_little() -> None:
    """Far past the limit the scan answers at once. Unbounded, the cost grew with the cube of
    the length: 3,200 parts took about 40 s to scan (1,600 about 5 s) when #395 measured it."""
    source = "import psycopg\n" + _chain("psycopg.connect", 3200)
    started = time.perf_counter()
    found = scan(source, RUNNER)
    assert time.perf_counter() - started < 5
    assert len(found) == 1
    assert LIMIT in found[0][1]
