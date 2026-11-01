"""Guard 5's annotation rule (#89, Architect's decision on #115): an allow-list type grammar.

Annotations run on Python 3.12: parameter, return and variable annotations when the ``def`` or
the statement runs, and every string in one when ``typing.get_type_hints`` resolves it. So an
annotation position may hold only a **type expression**:

- a name or a dotted name (no part starting with ``_``, never the issuer token's names);
- a subscript of a dotted name, whose subscript is again type expressions (tuples and lists of
  them included, for ``Callable[[int], str]``);
- ``X | Y`` of type expressions, and ``*Ts``;
- a constant (``None``, ``...``, numbers, strings);
- a string, which is parsed and checked as a type expression in its place, recursively. A
  string starting with ``*`` is checked as ``*<rest>`` (``"*Ts"``), as 3.12's ``ForwardRef``
  evaluates it. The grammar fails closed: a string that doesn't parse is refused.

Two subscripts are special:

- ``Literal[...]``'s arguments are values, never evaluated as types: they are not parsed, and
  may be constants and dotted names (``Color.RED``);
- ``Annotated[T, m1, m2]``: ``T`` is a type expression and ``m1, m2`` (the metadata) are ordinary
  code, the one place in an annotation where a call is allowed (FastAPI's ``Depends(...)``).

``Literal`` and ``Annotated`` count only when imported from ``typing`` or ``typing_extensions``
(any alias) and not rebound in the module. Anything else in a type position is refused: a call,
an attribute of a call, a lambda, a comprehension, a walrus, a subscript of a non-name.

The annotation positions: arguments, returns, variable annotations, the value of a
``X: TypeAlias = ...`` (``TypeAlias`` under any alias), and PEP 695 ``type`` aliases and
type-parameter bounds.

``splice_strings`` runs first and replaces each string in a type position with its parsed
expression **inside the module's own tree**, so every other rule of guard 5 judges the parsed
code (``Annotated`` metadata in a string above all) against the module's imports and aliases,
the same as code written without quotes.
"""

import ast

TYPING_MODULES = {"typing", "typing_extensions"}
MAX_DEPTH = 8  # strings inside strings; deeper nesting is refused


def typing_names(tree: ast.AST, name: str) -> set[str]:
    """The module's spellings of ``typing.<name>``: imported from ``typing`` or
    ``typing_extensions`` under any alias (``from typing import Annotated as A``; ``import typing
    as t`` gives ``t.Annotated``). A spelling whose root the module also binds another way (an
    assignment, a ``def`` or ``class``, another import) is not one."""
    names: set[str] = set()
    other: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in TYPING_MODULES:
                    names.add(f"{alias.asname or alias.name}.{name}")
                else:
                    other.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module in TYPING_MODULES and alias.name == name:
                    names.add(bound)
                else:
                    other.add(bound)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            other.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            other.add(node.name)
    return {spelling for spelling in names if spelling.split(".")[0] not in other}


def dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


class Spellings:
    def __init__(self, tree: ast.AST) -> None:
        self.annotated = typing_names(tree, "Annotated")
        self.literal = typing_names(tree, "Literal")
        self.type_alias = typing_names(tree, "TypeAlias")

    def is_annotated(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Subscript)
            and dotted(node.value) in self.annotated
            and isinstance(node.slice, ast.Tuple)
            and len(node.slice.elts) >= 2
        )

    def is_literal(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Subscript) and dotted(node.value) in self.literal


class _Splice(ast.NodeTransformer):
    """Replaces each string in a type position with its parsed expression."""

    def __init__(self, spellings: Spellings) -> None:
        self.spellings = spellings
        self.depth = 0
        self.refused: list[tuple[int, str]] = []
        self.strings: list[tuple[int, str]] = []  # every string met, before it is parsed

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if not isinstance(node.value, str):
            return node
        self.strings.append((node.lineno, node.value))
        if self.depth >= MAX_DEPTH:
            self.refused.append((node.lineno, "a string nested too deep in an annotation"))
            return node
        text = node.value.strip()
        # 3.12's ForwardRef evaluates a string starting with ``*`` as ``(<rest>,)[0]`` unpacked
        # (``"*Ts"``): the rest is a type expression, checked in a ``Starred``.
        starred = text.startswith("*")
        try:
            parsed = ast.parse(text[1:] if starred else text, mode="eval").body
        except SyntaxError:
            # Fails closed: a string the scan can't parse may still be evaluated.
            self.refused.append((node.lineno, "a string in a type position that doesn't parse"))
            return node
        if starred:
            parsed = ast.Starred(value=parsed, ctx=ast.Load())
        for sub in ast.walk(parsed):
            ast.copy_location(sub, node)  # report the string's own line
        self.depth += 1
        try:
            return self.visit(parsed)
        finally:
            self.depth -= 1

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        if self.spellings.is_literal(node):
            return node  # Literal's arguments are values, never parsed
        if self.spellings.is_annotated(node):
            assert isinstance(node.slice, ast.Tuple)
            node.slice.elts[0] = self.visit(node.slice.elts[0])
            return node  # the metadata is code: its strings are strings
        return self.generic_visit(node)


def _type_roots(tree: ast.AST, spellings: Spellings) -> list[tuple[ast.AST, str]]:
    """``(owner, field)`` of every annotation position in ``tree``."""
    roots: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
            roots.append((node, "annotation"))
            if (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and (
                    (dotted(node.annotation) or "").split(".")[-1] == "TypeAlias"
                    or dotted(node.annotation) in spellings.type_alias
                )
            ):
                roots.append((node, "value"))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
            roots.append((node, "returns"))
        elif isinstance(node, ast.TypeAlias):
            roots.append((node, "value"))
        elif isinstance(node, ast.TypeVar) and node.bound is not None:
            roots.append((node, "bound"))
    return roots


def splice_strings(
    tree: ast.AST, spellings: Spellings
) -> tuple[list[ast.AST], list[tuple[int, str]], list[tuple[int, str]]]:
    """Parse every string in a type position into ``tree`` in place. Returns the annotation
    roots (after splicing), what the splice refuses (a string that doesn't parse, or nested
    deeper than ``MAX_DEPTH``), and every string it met as written, for the string rules (a
    subscript key such as ``sys.modules["ownvitals.context"]`` reads as a name once parsed)."""
    splice = _Splice(spellings)
    roots: list[ast.AST] = []
    for owner, field in _type_roots(tree, spellings):
        root = splice.visit(getattr(owner, field))
        setattr(owner, field, root)
        roots.append(root)
    return roots, splice.refused, splice.strings


def metadata(root: ast.AST, spellings: Spellings) -> list[ast.expr]:
    """The ``Annotated`` metadata in ``root`` (code, not type), outside any other metadata."""
    found: list[ast.expr] = []
    pending: list[ast.AST] = [root]
    while pending:
        node = pending.pop()
        if spellings.is_annotated(node):
            assert isinstance(node, ast.Subscript)
            assert isinstance(node.slice, ast.Tuple)
            found += node.slice.elts[1:]
            pending.append(node.slice.elts[0])
            continue
        pending.extend(ast.iter_child_nodes(node))
    return found


def type_findings(
    root: ast.AST, spellings: Spellings, token_names: set[str]
) -> list[tuple[int, str]]:
    """What the grammar refuses in one (spliced) annotation root."""
    found: list[tuple[int, str]] = []

    def refuse(node: ast.AST, why: str) -> None:
        found.append((getattr(node, "lineno", 0), why))

    def name(node: ast.AST) -> bool:
        """A name or dotted name; refuses ``_`` parts and the token's names."""
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id in token_names:
                refuse(node, f"use of {node.id} in a type expression")
            return True
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in token_names:
                refuse(node, f"use of {node.attr} in a type expression")
            return name(node.value)
        return False

    def literal_argument(node: ast.AST) -> None:
        if isinstance(node, ast.Tuple):
            for element in node.elts:
                literal_argument(element)
        elif isinstance(node, ast.Constant):
            if node.value in token_names:
                refuse(node, "the issuer token's name in Literal")
        elif not (name(node) or spellings.is_literal(node)):
            refuse(node, f"{type(node).__name__} in Literal's arguments")

    def check(node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value in token_names:
                refuse(node, "the issuer token's name in a type expression")
        elif isinstance(node, ast.Name | ast.Attribute):
            if not name(node):
                refuse(node, "an attribute of a non-name in a type expression")
        elif isinstance(node, ast.Subscript):
            if not name(node.value):
                refuse(node, "a subscript of a non-name in a type expression")
            elif spellings.is_literal(node):
                literal_argument(node.slice)
            elif spellings.is_annotated(node):
                assert isinstance(node.slice, ast.Tuple)
                check(node.slice.elts[0])  # the metadata is code, judged by the other rules
            else:
                check(node.slice)
        elif isinstance(node, ast.Tuple | ast.List):
            for element in node.elts:
                check(element)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            check(node.left)
            check(node.right)
        elif isinstance(node, ast.Starred):
            check(node.value)
        else:
            refuse(node, f"{type(node).__name__} in a type expression")

    check(root)
    return found
