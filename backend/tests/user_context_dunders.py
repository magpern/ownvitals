"""Guard 5's attribute rule (#89, the Architect's decision on the eighth #115 review): dunder
names are an allow-list.

A name of the form ``__x__`` is refused wherever the source writes it so that it can name an
attribute, a module global or a builtin:

- an attribute (``f.__globals__``), also inside an f-string;
- a name (``__import__``, ``__builtins__``, ``__module__`` in a class body);
- a string constant (``getattr(f, "__module__")``, ``setattr``/``hasattr``/``delattr``,
  ``vars(K)["__init__"]``, ``operator.attrgetter("__dict__")``), f-string text included;
- a keyword argument (``f(__x__=v)``) and a ``match`` class-pattern keyword
  (``case object(__globals__=g)``, which reads the attribute);
- an imported name (``from m import __version__``) and a ``global``/``nonlocal`` declaration.

``ALLOWED`` holds the exceptions, each with its reason, and ``only_in`` where it is not allowed
everywhere: exactly what ``src`` (the issuer modules aside, which are not scanned) uses today,
and a name used in one module is allowed only there (``test_user_context_dunders`` pins both). An
exception lifts only this rule: the other rules of guard 5 still judge the same name
(``__init__`` only as ``super().__init__(...)``, ``object.__setattr__`` only in the
frozen-dataclass idiom).

Defining a method or a parameter with a dunder name is not naming an attribute, so ``def
__init__(self)`` stays allowed. A name built at run time (``"__glo" + "bals__"``) writes no
dunder: that is reflection, outside this rule's bound (``docs/dev/isolation.md``).
"""

import ast
import re
from dataclasses import dataclass

DUNDER = re.compile(r"__\w+__")


@dataclass(frozen=True)
class Allowed:
    reason: str
    only_in: frozenset[str] | None = None  # modules under ``src``; None means everywhere


LOG = frozenset({"ownvitals/log.py"})
ALLOWED: dict[str, Allowed] = {
    "__name__": Allowed("a module's or a function's name, for loggers and `__main__` checks"),
    "__main__": Allowed("the `if __name__ == '__main__'` entry points"),
    "__all__": Allowed("a package's public names"),
    "__init__": Allowed("`super().__init__(...)`; guard 5's hook rule refuses every other use"),
    "__aiter__": Allowed(
        "delegating `async for` to a wrapped stream", frozenset({"ownvitals/egress/clients.py"})
    ),
    "__cause__": Allowed("walking an exception chain", LOG),
    "__context__": Allowed("walking an exception chain", LOG),
    "__suppress_context__": Allowed("`raise ... from` hides the context on purpose", LOG),
    "__dict__": Allowed("a log record's extra fields", LOG),
    "__version__": Allowed(
        "the package version", frozenset({"ownvitals/__init__.py", "ownvitals/web.py"})
    ),
    "__code__": Allowed(
        "the egress hook recognises `socket.socketpair`'s own code",
        frozenset({"ownvitals/egress/hook.py"}),
    ),
    "__setattr__": Allowed(
        "`object.__setattr__(self, ...)` in a frozen dataclass's `__post_init__`; guard 5's "
        "idiom rule refuses every other use",
        frozenset({"ownvitals/analysis/methods/series.py", "ownvitals/analysis/quality.py"}),
    ),
    "__NAMES__": Allowed(
        "a placeholder in SQL text, replaced before the query runs",
        frozenset({"ownvitals/db/queue_check.py"}),
    ),
    "__SQL_TEXT_BUILTINS__": Allowed(
        "a placeholder in SQL text, replaced before the query runs",
        frozenset({"ownvitals/db/queue_check.py"}),
    ),
}


def is_dunder(name: str) -> bool:
    return DUNDER.fullmatch(name) is not None


def _named(node: ast.AST) -> list[str]:
    """The names ``node`` writes where they can name an attribute, a global or a builtin."""
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.keyword) and node.arg is not None:
        return [node.arg]
    if isinstance(node, ast.MatchClass):
        return list(node.kwd_attrs)
    if isinstance(node, ast.alias):
        return [*node.name.split("."), *([node.asname] if node.asname else [])]
    if isinstance(node, ast.Global | ast.Nonlocal):
        return list(node.names)
    return []


def allowed_in(name: str, module: str | None) -> bool:
    entry = ALLOWED.get(name)
    return entry is not None and (entry.only_in is None or module in entry.only_in)


def named_dunders(tree: ast.AST) -> list[tuple[int, str]]:
    """Every dunder ``tree`` names, allowed or not, with its line."""
    return [
        (getattr(node, "lineno", 0), name)
        for node in ast.walk(tree)
        for name in _named(node)
        if is_dunder(name)
    ]


def dunder_findings(tree: ast.AST, module: str | None) -> list[tuple[int, str]]:
    """Every dunder ``tree`` names outside ``ALLOWED`` (``module`` is the path under ``src``,
    ``ownvitals/log.py``; None for source that is no module of ``src``)."""
    return [
        (line, f"the dunder {name} (not allow-listed)")
        for line, name in named_dunders(tree)
        if not allowed_in(name, module)
    ]


def beyond_the_allow_list(found: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """``found`` without this rule's findings: what the other rules of guard 5 refuse."""
    return [(line, why) for line, why in found if not why.endswith("(not allow-listed)")]
