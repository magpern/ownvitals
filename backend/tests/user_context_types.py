"""Guard 5's annotation rule (#89, Architect's decision on #115): an allow-list type grammar.

Annotations run on Python 3.12: parameter, return and variable annotations when the ``def`` or
the statement runs, and every string in one when ``typing.get_type_hints`` resolves it. So an
annotation position may hold only a **type expression**:

- a name or a dotted name: a bare name may be a module's private name (``_Policy``) but not a
  dunder (``__builtins__``); an attribute part never starts with ``_``; never the token's names;
- a subscript of a dotted name, whose subscript is again type expressions (tuples and lists of
  them included, for ``Callable[[int], str]``);
- ``X | Y`` of type expressions, and ``*Ts``;
- a constant (``None``, ``...``, numbers, strings);
- a string, which is parsed and checked as a type expression in its place, recursively. A
  string starting with ``*`` is checked as ``*<rest>`` (``"*Ts"``), as 3.12's ``ForwardRef``
  evaluates it. The grammar fails closed: a string that doesn't parse is refused.

Two subscripts are special:

- ``Literal[...]``'s arguments are values, never evaluated as types: they are not parsed, and
  must be constants (or a nested ``Literal``). A name is refused (the thirteenth #115 review):
  ``Literal[ownvitals.context]`` hands the module to ``typing.get_args``;
- ``Annotated[T, m1, m2]``: ``T`` is a type expression and ``m1, m2`` (the metadata) are ordinary
  code, the one place in an annotation where a call is allowed (FastAPI's ``Depends(...)``).

``Literal`` and ``Annotated`` count only when imported from ``typing`` or ``typing_extensions``
(any alias) and when that import is the sole binding of the name in every scope (``symtable``
and the AST's binding sites agree; see ``typing_names``). A subscript named ``Literal`` or
``Annotated`` that doesn't resolve so is refused: a misread exemption fails closed. Anything else
in a type position is refused: a call, an attribute of a call, a lambda, a comprehension, a
walrus, a subscript of a non-name.

The annotation positions: arguments, returns, variable annotations, the value of a
``X: TypeAlias = ...`` (``TypeAlias`` under any alias), and PEP 695 ``type`` aliases and
type-parameter bounds.

``splice_strings`` runs first and replaces each string in a type position with its parsed
expression **inside the module's own tree**, so every other rule of guard 5 judges the parsed
code (``Annotated`` metadata in a string above all) against the module's imports and aliases,
the same as code written without quotes.
"""

import ast
import symtable

TYPING_MODULES = {"typing", "typing_extensions"}
MAX_DEPTH = 8  # strings inside strings; deeper nesting is refused


BINDS_ITS_NAME = (
    ast.TypeVar,
    ast.ParamSpec,
    ast.TypeVarTuple,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


def _binding_sites(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """Every place ``tree`` binds a name, in any scope: imports, assignments and deletions
    (comprehension variables and the walrus included), parameters (lambdas too), ``except ...
    as``, ``match`` captures, PEP 695 type parameters, ``def``/``class`` names, and ``global``/
    ``nonlocal`` declarations (counted, so a name declared so is never trusted)."""
    sites: dict[str, list[ast.AST]] = {}

    def add(name: str | None, node: ast.AST) -> None:
        if name is not None:
            sites.setdefault(name, []).append(node)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            add(node.id, node)
        elif isinstance(node, ast.arg):
            add(node.arg, node)
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar):
            add(node.name, node)
        elif isinstance(node, ast.MatchMapping):
            add(node.rest, node)
        elif isinstance(node, BINDS_ITS_NAME):
            add(node.name, node)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            for name in node.names:
                add(name, node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.asname or alias.name.split(".")[0], node)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                add(alias.asname or alias.name, node)
    return sites


def _rebound_in_a_scope(table: symtable.SymbolTable, name: str) -> bool:
    """The compiler's view: ``name`` is a parameter, assigned, deleted, or a ``def``/``class``
    in some scope of the module."""
    pending = [table]
    while pending:
        scope = pending.pop()
        pending.extend(scope.get_children())
        try:
            symbol = scope.lookup(name)
        except KeyError:
            continue
        if symbol.is_parameter() or symbol.is_assigned() or symbol.is_namespace():
            return True
    return False


def _foreign_star_import(tree: ast.AST) -> bool:
    """``from <module> import *`` from a module other than ``typing`` or
    ``typing_extensions``."""
    return any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "*" for alias in node.names)
        and (node.level > 0 or node.module not in TYPING_MODULES)
        for node in ast.walk(tree)
    )


def typing_names(tree: ast.AST, table: symtable.SymbolTable, name: str) -> set[str]:
    """The module's spellings of ``typing.<name>``: imported from ``typing`` or
    ``typing_extensions`` under any alias (``from typing import Annotated as A``; ``import typing
    as t`` gives ``t.Annotated``). A spelling counts only if every binding of its root, in every
    scope, is that import: ``symtable`` (parameters, assignments, ``def``/``class``) and the
    AST's binding sites (every form, comprehension variables and ``global`` included) agree.
    A star import from any other module binds names neither view sees, so it counts as a
    binding of every name: the module then has no spelling of ``typing.<name>``."""
    if _foreign_star_import(tree):
        return set()
    sites = _binding_sites(tree)
    found: set[str] = set()

    def is_typing_import(node: ast.AST, root: str, direct: bool) -> bool:
        if direct and isinstance(node, ast.ImportFrom):
            return node.module in TYPING_MODULES and all(
                a.name == name for a in node.names if (a.asname or a.name) == root
            )
        if not direct and isinstance(node, ast.Import):
            return all(
                a.name in TYPING_MODULES
                for a in node.names
                if (a.asname or a.name.split(".")[0]) == root
            )
        return False

    for node in ast.walk(tree):
        candidates: list[tuple[str, str, bool]] = []
        if isinstance(node, ast.Import):
            candidates = [
                (a.asname or a.name, f"{a.asname or a.name}.{name}", False)
                for a in node.names
                if a.name in TYPING_MODULES
            ]
        elif isinstance(node, ast.ImportFrom) and node.module in TYPING_MODULES:
            candidates = [
                (a.asname or a.name, a.asname or a.name, True) for a in node.names if a.name == name
            ]
        for root, spelling, direct in candidates:
            if not _rebound_in_a_scope(table, root) and all(
                is_typing_import(site, root, direct) for site in sites.get(root, [])
            ):
                found.add(spelling)
    return found


def import_aliases(tree: ast.AST, name: str) -> set[str]:
    """Every local name an import binds to ``<module>.<name>``, whatever the module: a
    ``Literal`` or ``Annotated`` under another name that doesn't resolve is still refused."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == name
    }


def dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


EXEMPT = ("Literal", "Annotated")  # the two subscripts the grammar treats specially


class Spellings:
    def __init__(self, tree: ast.AST, source: str) -> None:
        table = symtable.symtable(source, "<scan>", "exec")
        self.annotated = typing_names(tree, table, "Annotated")
        self.literal = typing_names(tree, table, "Literal")
        self.type_alias = typing_names(tree, table, "TypeAlias")
        self.exempt_aliases = import_aliases(tree, "Literal") | import_aliases(tree, "Annotated")

    def is_unresolved(self, node: ast.AST) -> bool:
        """A subscript named ``Literal`` or ``Annotated`` (by its last name part, or an imported
        alias of either) that doesn't resolve to ``typing``'s: refused, never a plain generic."""
        if not isinstance(node, ast.Subscript) or self.is_literal(node) or self.is_annotated(node):
            return False
        spelling = dotted(node.value)
        return spelling is not None and (
            spelling.split(".")[-1] in EXEMPT or spelling in self.exempt_aliases
        )

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
            # A module's own private names (``_Policy``, ``_T``) are fine as types; a dunder
            # name (``__builtins__``) or the token never is.
            if (node.id.startswith("__") and node.id.endswith("__")) or node.id in token_names:
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
        elif not spellings.is_literal(node):
            refuse(node, f"{type(node).__name__} in Literal's arguments (constants only)")

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
            elif spellings.is_unresolved(node):
                refuse(node, "a Literal or Annotated that isn't typing's (rebound or not imported)")
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
