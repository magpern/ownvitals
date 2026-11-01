"""Importers for the egress guard (``egress_scan``; #122 and the FAILs on #123). Fail closed.

- ``IMPORT_CALLS`` (``import_module``, ``__import__``) may be referenced only as the callee of a
  direct call with one string literal, which ``egress_scan`` then judges as an import; any other
  reference (stored, in a list, through ``getattr``) would hide the name.
- ``LOADER_MODULES`` (``runpy``, ``pkgutil``, ``zipimport``, ``importlib.machinery``, the
  bootstrap, ``importlib.abc``, ``_imp``) and every ``importlib`` name but ``import_module`` and
  ``importlib.metadata`` load a module by a name the guard cannot judge: refused outright.
- ``EVALUATORS`` (``exec``, ``eval``, ``compile``, ``pickle``, ``marshal``, ``shelve``,
  ``timeit``, ``code``, ``codeop``, pydantic's ``ImportString``) run code or load objects from
  data the guard cannot read: refused outright, under any alias or ``from`` form.
- ``names``: a ``str``/``bytes`` constant naming a native or process module as an identifier
  segment (docstrings aside), the strings ``"__import__"``/``"import_module"`` and the name
  ``__builtins__``, the forms every other importer would take (``__builtins__["__import__"]``,
  ``vars(builtins)["__import__"]``, ``pkgutil.get_loader("ctypes")``, ``"ctypes:CDLL"``).

A name built at run time (``"cty" + "pes"``) is reflection, left to review (``egress.md``).
Each function takes ``landings``: a dotted name and where import bindings take it.
"""

import ast
import re
from collections.abc import Callable

from egress_classified import (
    CODE_STRINGS,
    CODE_WORDS,
    EVALUATORS,
    IMPORT_CALLS,
    LOADER_MODULES,
    NATIVE_MODULES,
    PROCESS_MODULES,
    SOURCE_READERS,
    SPEC_FINDERS,
)
from egress_closure import under
from egress_forward import holds_code
from egress_typeonly import dotted

Landings = Callable[[str], frozenset[str]]
NOT_DIRECT = "an importer is only called directly, with one constant name"
_IMPORTLIB_ALLOWED = ("importlib", "importlib.import_module")
_IMPORTER_STRINGS = frozenset({"__import__", "import_module"})
_BUILTINS = "__builtins__"
_READ_BACK = {
    _BUILTINS: "reaches every importer by name",
    "__doc__": "reads a docstring back as a string",
}
_SEPARATORS = re.compile(r"[^A-Za-z0-9_]+")


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


def evaluator(name: str, landings: Landings) -> bool:
    """Whether ``name`` lands on code or data evaluation (``EVALUATORS``): ``exec``, ``eval``,
    ``compile``, ``pickle``, ``marshal``, ``shelve``, ``timeit``, ``code``, ``ImportString``."""
    return any(under(n, EVALUATORS) for n in landings(name))


def reads_source(name: str, landings: Landings) -> bool:
    """Whether ``name`` lands in a module that hands back source text (``SOURCE_READERS``)."""
    return any(under(n, SOURCE_READERS) for n in landings(name))


def _standalone(tree: ast.AST) -> set[int]:
    """``id``s of the strings that are a statement of their own: docstrings and the like."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }


def _segments(value: str | bytes) -> list[str]:
    text = value.decode("latin-1") if isinstance(value, bytes) else value
    return [s for s in _SEPARATORS.split(text) if s]


def _names_module(value: str | bytes) -> str | None:
    """The native or process module ``value`` names as identifier segments, if any:
    ``"ctypes:CDLL"``, ``"import ctypes"`` and ``"ctypes/__init__.py"`` do, ``"ctypesish"``
    does not (a pickle opcode glued to a name does not either: ``pickle`` is refused by name)."""
    segments = _segments(value)
    for module in NATIVE_MODULES + PROCESS_MODULES:
        parts = module.split(".")
        for i in range(len(segments) - len(parts) + 1):
            if segments[i : i + len(parts)] == parts:
                return module
    return None


def _string(value: str | bytes, module: str) -> str | None:
    """Why a ``str``/``bytes`` constant is refused: it names a native or process module or a
    code word as identifier segments, or (a ``str``) parses as more than names (``holds_code``),
    unless ``CODE_STRINGS`` lists it for ``module``."""
    named = _names_module(value)
    if named is not None:
        return f"names {named!r}: a native or process module, by string"
    if isinstance(value, str) and (module, value) in CODE_STRINGS:
        return None
    words = sorted(CODE_WORDS.intersection(_segments(value)))
    if words or value in _IMPORTER_STRINGS:
        return f"names {words[0] if words else value!r}: an importer or evaluator, by string"
    if isinstance(value, str) and holds_code(value):
        return f"string {value[:40]!r} parses as code: typing or pydantic could evaluate it"
    return None


def names(tree: ast.AST, skip: set[int], module: str) -> list[tuple[int, str]]:
    """``(line, why)`` for each refused ``str`` or ``bytes`` constant (``_string``), and each use
    of ``__builtins__`` or ``__doc__``. Docstrings and the strings ``skip`` (``egress_forward``
    reads them) are not judged here; ``__doc__`` and ``SOURCE_READERS`` keep them unread."""
    found: list[tuple[int, str]] = []
    skip = skip | _standalone(tree)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        line = getattr(node, "lineno", 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes):
            why = _string(node.value, module)
            if why is not None:
                found.append((line, why))
        elif (spelled := getattr(node, "id", None) or getattr(node, "attr", None)) in _READ_BACK:
            found.append((line, f"uses {spelled}: {_READ_BACK[str(spelled)]}"))
    return found
