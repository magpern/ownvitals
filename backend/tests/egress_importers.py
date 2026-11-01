"""Importers for the egress guard (``egress_scan``; #122 and the FAILs on #123). Fail closed.

- ``IMPORT_CALLS`` (``import_module``, ``__import__``) may be referenced only as the callee of a
  direct call with one string literal, which ``egress_scan`` then judges as an import; any other
  reference (stored, in a list, through ``getattr``) would hide the name.
- ``LOADER_MODULES`` (``runpy``, ``pkgutil``, ``zipimport``, ``importlib.machinery``, the
  bootstrap, ``importlib.abc``, ``_imp``) and every ``importlib`` name but ``import_module`` and
  ``importlib.metadata`` load a module by a name the guard cannot judge: refused outright.
- ``names``: a string constant naming a native or process module (or under one), the strings
  ``"__import__"``/``"import_module"`` and the name ``__builtins__``, the forms every other
  importer would take (``__builtins__["__import__"]``, ``vars(builtins)["__import__"]``,
  ``pkgutil.get_loader("ctypes")``, ``runpy.run_module("ctypes.wintypes")``).

A name built at run time (``"cty" + "pes"``) is reflection, left to review (``egress.md``).
Each function takes ``landings``: a dotted name and where import bindings take it.
"""

import ast
from collections.abc import Callable

from egress_classified import (
    IMPORT_CALLS,
    LOADER_MODULES,
    NATIVE_MODULES,
    PROCESS_MODULES,
    SPEC_FINDERS,
)
from egress_closure import under
from egress_typeonly import dotted

Landings = Callable[[str], frozenset[str]]
NOT_DIRECT = "an importer is only called directly, with one constant name"
_IMPORTLIB_ALLOWED = ("importlib", "importlib.import_module")
_IMPORTER_STRINGS = frozenset({"__import__", "import_module"})
_BUILTINS = "__builtins__"


def importer(name: str, landings: Landings, calls_only: bool = False) -> bool:
    """Whether ``name`` lands on an importer; ``calls_only``: one that may be called directly."""
    for n in landings(name):
        if n in IMPORT_CALLS or (not calls_only and under(n, SPEC_FINDERS)):
            return True
    return False


def loader(name: str, landings: Landings) -> bool:
    """Whether ``name`` lands in a module loader or an ``importlib`` name outside the two."""
    for n in landings(name):
        if under(n, LOADER_MODULES):
            return True
        allowed = n in _IMPORTLIB_ALLOWED or under(n, ("importlib.metadata",))
        if under(n, ("importlib",)) and not allowed:
            return True
    return False


def direct_imports(tree: ast.AST, aliases: dict[str, str], landings: Landings) -> set[int]:
    """``id``s of the callees of direct ``import_module``/``__import__`` calls: the one way an
    importer may be referenced (``dynamic_import`` then judges the name)."""
    return {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and importer(dotted(node.func, aliases) or "", landings, True)
    }


def dynamic_import(
    node: ast.Call, aliases: dict[str, str], landings: Landings
) -> list[tuple[int, str, bool]] | str:
    """A constant ``import_module``/``__import__`` as an import, or why it is refused. A bare
    ``import_module``/``__import__`` callee counts even when unresolved (``x.import_module``)."""
    func = node.func
    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    resolved = importer(dotted(func, aliases) or "", landings, True)
    if called not in ("import_module", "__import__") and not resolved:
        return []
    first = node.args[0] if node.args else None
    extra = node.args[1:] or [k for k in node.keywords if k.arg in ("package", "fromlist", None)]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)) or extra:
        return f"imports dynamically ({called}): a module name must be one constant"
    return [(node.lineno, first.value, True)]


def _string_annotations(tree: ast.AST) -> set[int]:
    """``id``s of the annotations that are one string (``x: "ctypes.c_int"``): a forward
    reference, kept as a string at run time. A string nested inside an annotation is not one."""
    roots: list[ast.expr | None] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            roots.append(node.returns)
        elif isinstance(node, ast.arg | ast.AnnAssign):
            roots.append(node.annotation)
    return {id(r) for r in roots if isinstance(r, ast.Constant)}


def names(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, why)`` for each string naming a native or process module or an importer, and
    each use of ``__builtins__``."""
    found: list[tuple[int, str]] = []
    forward = _string_annotations(tree)
    for node in ast.walk(tree):
        if id(node) in forward:
            continue
        line = getattr(node, "lineno", 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if under(text, NATIVE_MODULES + PROCESS_MODULES):
                found.append((line, f"names {text!r}: a native or process module, by string"))
            elif text in _IMPORTER_STRINGS or text == _BUILTINS:
                found.append((line, f"names {text!r}: an importer, by string"))
        elif isinstance(node, ast.Name) and node.id == _BUILTINS:
            found.append((line, f"uses {_BUILTINS}: reaches every importer by name"))
        elif isinstance(node, ast.Attribute) and node.attr == _BUILTINS:
            found.append((line, f"uses .{_BUILTINS}: reaches every importer by name"))
    return found
