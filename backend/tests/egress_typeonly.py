"""Type-only imports for the egress guard (``egress_scan``; #122).

An ``if TYPE_CHECKING:`` block is treated as never running only when its test is ``typing``'s or
``typing_extensions``'s flag (``TYPE_CHECKING`` imported from either, by any alias, or the dotted
``typing.TYPE_CHECKING``) and nothing in the module rebinds it. A name rebound by any binding
``symtable`` records in a scope of the module (assignment, ``del``, parameter, walrus, loop or
``with`` target, ``except ... as`` ...), or bound by an import from elsewhere, makes the
block ordinary code, scanned as such. Independently, ``assignments`` refuses any write to a name
or attribute ``TYPE_CHECKING`` and the string ``"TYPE_CHECKING"`` or keyword ``TYPE_CHECKING=``
(``globals()``, ``vars()``, ``setattr`` by any spelling); any of them trusts no block.

Inside a type-only block an import binds names that may appear only in annotations
(``annotations``); ``egress_scan`` resolves those through the run-time imports alone.
"""

import ast
import symtable

FLAG = "TYPE_CHECKING"
TYPING = frozenset({"typing", "typing_extensions"})
_FLAGS = frozenset(f"{m}.{FLAG}" for m in TYPING)


def dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    head = aliases.get(node.id)
    return None if head is None else ".".join([head, *reversed(parts)])


def _binders(tree: ast.AST, name: str) -> set[str]:
    """What every import that binds ``name`` binds it to (``?`` for a relative import)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if (a.asname or a.name.split(".")[0]) == name:
                    found.add(a.name if a.asname else a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            source = node.module if node.level == 0 else "?"
            found |= {f"{source}.{a.name}" for a in node.names if (a.asname or a.name) == name}
    return found


def _rebound(table: symtable.SymbolTable, name: str) -> bool:
    """Whether any scope binds ``name`` other than by an import (``_binders`` judges those). A
    ``global`` or ``nonlocal`` write is an assignment in its own scope."""
    for symbol in table.get_symbols():
        if symbol.get_name() == name and (symbol.is_assigned() or symbol.is_parameter()):
            return True
    return any(_rebound(child, name) for child in table.get_children())


def _is_flag(test: ast.expr, tree: ast.AST, table: symtable.SymbolTable) -> bool:
    if isinstance(test, ast.Name):
        name, wanted = test.id, _FLAGS
    elif isinstance(test, ast.Attribute) and test.attr == FLAG and isinstance(test.value, ast.Name):
        name, wanted = test.value.id, TYPING
    else:
        return False
    binders = _binders(tree, name)
    return bool(binders) and binders <= wanted and not _rebound(table, name)


def type_only(tree: ast.AST, source: str) -> set[int]:
    """``id``s of the import nodes inside a trusted ``if TYPE_CHECKING:`` block; the ``else``
    branch runs and is not one. Any write to ``TYPE_CHECKING`` trusts no block."""
    if assignments(tree):
        return set()
    table = symtable.symtable(source, "<scanned>", "exec")
    nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_flag(node.test, tree, table):
            for statement in node.body:
                nodes.update(
                    id(n) for n in ast.walk(statement) if isinstance(n, ast.Import | ast.ImportFrom)
                )
    return nodes


def annotations(tree: ast.AST) -> set[int]:
    """``id``s of every node inside an annotation: the only place a name imported for type
    checking may appear (anywhere else it would run, and could only work if rebound)."""
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
            roots.append(node.returns)
        elif isinstance(node, ast.arg | ast.AnnAssign) and node.annotation:
            roots.append(node.annotation)
    return {id(inner) for root in roots for inner in ast.walk(root)}


def _bound(node: ast.AST) -> list[str]:
    """The names or attributes ``node`` writes (imports aside: they are judged by ``_is_flag``)."""
    if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Store | ast.Del):
        return [node.id if isinstance(node, ast.Name) else node.attr]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
        return [node.name or ""]
    if isinstance(node, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple):
        return [node.name]
    if isinstance(node, ast.MatchMapping):
        return [node.rest or ""]
    if isinstance(node, ast.Global | ast.Nonlocal):
        return node.names
    return []


def _named(node: ast.AST) -> bool:
    """The flag as a string or a keyword: how ``globals()``, ``vars()``, ``__dict__`` and every
    ``setattr`` spelling would write it (the FAIL on #123, B2)."""
    if isinstance(node, ast.Constant):
        return node.value == FLAG
    return isinstance(node, ast.keyword) and node.arg == FLAG


def assignments(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, why)`` for every write to a name or attribute ``TYPE_CHECKING``, and every use of
    it as a string or a keyword."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 1)
        if FLAG in _bound(node):
            found.append((line, f"assigns {FLAG}: a type-only block must never run"))
        elif _named(node):
            found.append((line, f"names {FLAG} as a string or keyword: it could write the flag"))
    return found
