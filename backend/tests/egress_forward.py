"""Strings that ``typing`` evaluates, for the egress guard (``egress_scan``; the FAILs on #123).

``typing.get_type_hints``, which FastAPI and pydantic call on routes and models (pydantic when it
builds the class), evaluates every string in an annotation as code: a whole-string annotation
(``x: "Node"``) and a string nested in one (``list["Node"]``, ``Optional[...]``,
``Annotated["Node", ...]``), a string ``TypeVar`` bound or constraint and ``ForwardRef("...")``.
Each such string may hold only names, dotted names, subscripts, tuples, lists, constants and
``X | Y`` (a string nested in it likewise); ``egress_scan`` then judges each name in it like a
name in code. A ``Literal[...]`` argument is a value and ``Annotated`` metadata is not a type: they
are not read. An annotation that builds a string (an f-string, ``+`` or any operator but ``|``) is
refused: ``get_type_hints`` would evaluate the string it builds.
"""

import ast

from egress_typeonly import dotted

_TYPING = ("typing", "typing_extensions")
_LITERAL = frozenset(f"{m}.Literal" for m in _TYPING)
_ANNOTATED = frozenset(f"{m}.Annotated" for m in _TYPING)
_TYPEVAR = frozenset(f"{m}.TypeVar" for m in _TYPING)
_FORWARDREF = frozenset(f"{m}.ForwardRef" for m in _TYPING)
_NODES = (ast.Expression, ast.Name, ast.Attribute, ast.Subscript, ast.Tuple, ast.List)
_NODES += (ast.Constant, ast.Load, ast.BitOr)
RUNS = "a string annotation holds more than names: it runs"
BUILDS = "an annotation builds a string: get_type_hints would run it"


def _roots(tree: ast.AST, aliases: dict[str, str]) -> list[ast.expr]:
    """Every annotation, and every string a ``TypeVar`` bound, constraint or ``ForwardRef``
    hands to ``typing``."""
    roots: list[ast.expr | None] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            roots.append(node.returns)
        elif isinstance(node, ast.arg | ast.AnnAssign):
            roots.append(node.annotation)
        elif isinstance(node, ast.Call):
            called = dotted(node.func, aliases) or ""
            if called in _TYPEVAR:
                roots += node.args[1:] + [k.value for k in node.keywords if k.arg == "bound"]
            elif called in _FORWARDREF:
                roots += node.args[:1]
    return [r for r in roots if r is not None]


def _strings(node: ast.expr, aliases: dict[str, str], out: list[ast.expr]) -> None:
    """The strings in a type expression (``out``), and each node that builds one."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            out.append(node)
    elif isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.BinOp) and not isinstance(node.op, ast.BitOr)
    ):
        out.append(node)  # refused by ``forward_references``
    elif isinstance(node, ast.BinOp):
        _strings(node.left, aliases, out)
        _strings(node.right, aliases, out)
    elif isinstance(node, ast.Tuple | ast.List):
        for element in node.elts:
            _strings(element, aliases, out)
    elif isinstance(node, ast.Subscript):
        generic = dotted(node.value, aliases) or ""
        if generic in _LITERAL:
            return
        inner = node.slice
        if generic in _ANNOTATED and isinstance(inner, ast.Tuple) and inner.elts:
            inner = inner.elts[0]
        _strings(inner, aliases, out)


def _forward(text: str, depth: int = 0) -> list[ast.expr] | None:
    """The name nodes of a forward reference, or ``None`` when it holds more than names."""
    try:
        parsed = ast.parse(text, mode="eval")
    except SyntaxError:
        return None
    found: list[ast.expr] = []
    for node in ast.walk(parsed):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            continue
        if not isinstance(node, _NODES):
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            inner = _forward(node.value, depth + 1) if depth < 8 else None
            if inner is None:
                return None
            found += inner
        elif isinstance(node, ast.Name | ast.Attribute):
            found.append(node)
    return found


_RUNS = (ast.Call, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_RUNS += (ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom, ast.JoinedStr)


def holds_code(text: str, depth: int = 0) -> bool:
    """Whether ``text`` parses as an expression that runs code if ``typing`` or pydantic
    evaluate it: a call, a lambda, a comprehension, a walrus, an f-string, or a string in it that
    does (every import needs a call). Text that does not parse (prose, SQL) does not; operators
    and comparisons on names (``"no-store"``, ``"utf-8"``) only read names."""
    try:
        parsed = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(parsed):
        if isinstance(node, _RUNS):
            return True
        value = node.value if isinstance(node, ast.Constant) else None
        if isinstance(value, str) and depth < 8 and holds_code(value, depth + 1):
            return True
    return False


def spelled(node: ast.expr) -> str | None:
    """A name or dotted name as written (``ctypes.c_int``), with no alias resolved."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def forward_references(
    tree: ast.AST, aliases: dict[str, str]
) -> tuple[list[tuple[int, str]], list[tuple[int, ast.expr]], set[int]]:
    """``(line, why)`` for each string ``typing`` would evaluate that holds more than names, or
    each annotation that builds one; ``(line, node)`` for each name in the rest, for
    ``egress_scan`` to judge; and the ``id``s of the strings read here."""
    refused: list[tuple[int, str]] = []
    references: list[tuple[int, ast.expr]] = []
    read: set[int] = set()
    for root in _roots(tree, aliases):
        found: list[ast.expr] = []
        _strings(root, aliases, found)
        for node in found:
            read.add(id(node))
            if not isinstance(node, ast.Constant):
                refused.append((node.lineno, BUILDS))
                continue
            names = _forward(str(node.value))
            if names is None:
                refused.append((node.lineno, RUNS))
            else:
                references += [(node.lineno, name) for name in names]
    return refused, references, read
