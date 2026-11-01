"""ADR 0009, the inherited connection scope: a task created or a ``to_thread`` call made inside a
connection scope keeps the scope for as long as it runs. So the code that opens a scope (the
egress package and mail) never starts concurrent work inside one. A static check of those
modules, and proof that it fails."""

import ast
from pathlib import Path

import pytest

from egress_scan import sources

SRC = Path(__file__).resolve().parent.parent / "src"
SCOPES = frozenset({"_connection_scope", "smtp_session"})
# What starts work that outlives the call or runs beside it with a copy of the context.
STARTS = frozenset(
    {
        "TaskGroup",
        "Thread",
        "create_task",
        "ensure_future",
        "gather",
        "run_coroutine_threadsafe",
        "run_in_executor",
        "start_soon",
        "submit",
        "to_thread",
    }
)


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def starts_inside_a_scope(source: str) -> list[tuple[int, str]]:
    """``(line, name)`` for each call in ``STARTS`` inside a ``with`` that opens a scope."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        opens = any(
            isinstance(item.context_expr, ast.Call) and _name(item.context_expr.func) in SCOPES
            for item in node.items
        )
        if not opens:
            continue
        for statement in node.body:
            for inner in ast.walk(statement):
                if isinstance(inner, ast.Call) and (name := _name(inner.func)) in STARTS:
                    found.append((inner.lineno, name))
    return found


def test_no_scope_starts_concurrent_work() -> None:
    found = {
        module: hits
        for module, source in sources(SRC)
        if (module.startswith("ownvitals.egress") or module == "ownvitals.mail")
        and (hits := starts_inside_a_scope(source))
    }
    assert found == {}


def test_the_scopes_are_where_the_check_looks() -> None:
    opened = {
        module
        for module, source in sources(SRC)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _name(node.func) in SCOPES
    }
    assert opened == {
        "ownvitals.egress.clients",
        "ownvitals.egress.opener",
        "ownvitals.egress.hook",
        "ownvitals.mail",
    }


@pytest.mark.parametrize(
    "source",
    [
        "with _connection_scope('httpx'):\n    asyncio.create_task(send())",
        "async def f():\n    with smtp_session():\n        await asyncio.to_thread(g)",
        "with hook._connection_scope('x'), open(p):\n    loop.run_in_executor(None, g)",
        "with smtp_session():\n    if x:\n        threading.Thread(target=g).start()",
        "with _connection_scope('x'):\n    async with asyncio.TaskGroup() as tg:\n        pass",
    ],
)
def test_the_check_fails_on_work_started_inside_a_scope(source: str) -> None:
    assert starts_inside_a_scope(source) != []


def test_the_check_allows_plain_calls_inside_a_scope() -> None:
    source = "with _connection_scope('httpx'):\n    response = await inner.handle(request)"
    assert starts_inside_a_scope(source) == []
