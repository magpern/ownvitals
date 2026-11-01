"""Guard 5's rules for the issuer token's other routes (#89), used by ``user_context_scan``.

``findings(tree)`` refuses, in every scanned module (the issuer modules are not scanned), type
annotations included:

- the attribute, name or string ``__globals__``, ``__module__``, ``__kwdefaults__`` and
  ``__defaults__``: a function's globals hold the issuer token (``require_context.__globals__``),
  ``sys.modules[f.__module__]`` is the module that holds it, and a default can pass it;
- the module object ``ownvitals.context``: every import that binds it (``import
  ownvitals.context as c``, ``from ownvitals import context``, a relative ``from .. import
  context``), ``ownvitals.context`` reached as an attribute of the package outside a type
  annotation, and the strings ``"ownvitals.context"`` and ``".context"`` (``sys.modules[...]``,
  ``sys.modules.get(...)``, ``importlib.import_module``, ``__import__``). With the module,
  ``vars(c)`` or ``c.__dict__`` hold the token. A plain ``import ownvitals.context`` binds only
  the package, like ``import ownvitals.db``, so it stays allowed for annotations and its use is
  judged instead. Importing names from the module (``from ownvitals.context import
  require_context``) stays allowed: the scan judges each name. A star import from
  ``ownvitals`` or its packages (relative ones included) is refused: it binds ``context``;
- an ``issuer=`` keyword in any call, and a ``**`` argument that spells ``"issuer"`` anywhere in
  it (``**{"issuer": t}``, ``**{k: t for k in ["issuer"]}``, ``**dict([("issuer", t)])``,
  ``**({} | {"issuer": t})``): only the issuer modules pass the token;
- ``copyreg`` as an import, a name, an attribute or a string: ``copyreg.__newobj__(K)`` makes a
  blank context without the token.

``annotation_findings`` keeps annotations, which run on Python 3.12, to type expressions (see
its docstring).

Threat model (``docs/dev/isolation.md``): these rules stop plausible, review-visible forgeries;
they are not a sandbox. Reflection in the process (the package object, ``sys.modules``, ``gc``,
``inspect``, ``ctypes``), names built at run time and a keyword dict bound to a name first stay
with code review, RLS and the role checks (ADR 0002 section 1).
"""

import ast

CONTEXT_MODULE = "ownvitals.context"
PACKAGE = "ownvitals"
# Never named: ``__globals__`` and ``__module__`` lead to the token; ``__kwdefaults__`` and
# ``__defaults__`` would make it a default (``K.__init__.__kwdefaults__["issuer"] = tok``);
# ``copyreg`` builds a blank instance without calling ``__init__``.
NEVER_NAMED = {"__globals__", "__module__", "__kwdefaults__", "__defaults__", "copyreg"}
# Strings that name the context module, for ``sys.modules`` and ``importlib``.
MODULE_STRINGS = {CONTEXT_MODULE, ".context"}
# The issuer token's keyword (``UserContext(..., issuer=...)``).
ISSUER_KEYWORD = "issuer"
# ``UserContext``'s fields: the frozen-dataclass idiom never sets one of these names.
# ``test_user_context_writers.py`` pins this set to the class.
CONTEXT_FIELDS = {"user_id", "role", "connections"}


def passes_issuer(node: ast.Call) -> bool:
    """``f(..., issuer=x)``, or a ``**`` argument that spells ``"issuer"`` anywhere in it."""
    for keyword in node.keywords:
        if keyword.arg == ISSUER_KEYWORD:
            return True
        if keyword.arg is None and any(
            isinstance(sub, ast.Constant) and sub.value == ISSUER_KEYWORD
            for sub in ast.walk(keyword.value)
        ):
            return True
    return False


def _within(name: str | None, module: str) -> bool:
    return name is not None and (name == module or name.startswith(f"{module}."))


def _import_finding(node: ast.Import | ast.ImportFrom) -> str | None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if _within(alias.name, "copyreg"):
                return "import of copyreg"
            if _within(alias.name, CONTEXT_MODULE) and alias.asname:
                return f"import of the module {CONTEXT_MODULE}"
        return None
    if _within(node.module, "copyreg"):
        return "import of copyreg"
    if any(a.name == "*" for a in node.names) and (node.level > 0 or _within(node.module, PACKAGE)):
        # ``ownvitals`` has no ``__all__``: a star import binds every loaded submodule, the
        # module object ``context`` among them.
        return "star import from ownvitals"
    for alias in node.names:
        if alias.name == "context" and (node.level > 0 or node.module == PACKAGE):
            return f"import of the module {CONTEXT_MODULE}"
    return None


def _package_names(tree: ast.AST) -> set[str]:
    """Local names bound to the ``ownvitals`` package (``import ownvitals``, ``import
    ownvitals.db``, ``import ownvitals as o``)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PACKAGE and alias.asname:
                    names.add(alias.asname)
                elif _within(alias.name, PACKAGE) and not alias.asname:
                    names.add(PACKAGE)
    return names


def findings(tree: ast.AST, annotation_nodes: set[int]) -> list[tuple[int, str]]:
    """What these rules refuse in ``tree``, annotations included (they run on 3.12).
    ``annotation_nodes`` are the ids of the nodes inside type annotations, where the package
    attribute ``ownvitals.context`` may name a type (``ownvitals.context.UserContext``)."""
    found: list[tuple[int, str]] = []
    package = _package_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            if (why := _import_finding(node)) is not None:
                found.append((node.lineno, why))
        elif isinstance(node, ast.Call) and passes_issuer(node):
            found.append((node.lineno, "an issuer= keyword outside the issuer modules"))
        elif isinstance(node, ast.Name) and node.id in NEVER_NAMED:
            found.append((node.lineno, f"use of {node.id}"))
        elif isinstance(node, ast.Attribute) and node.attr in NEVER_NAMED:
            found.append((node.lineno, f"use of {node.attr}"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "context"
            and isinstance(node.value, ast.Name)
            and node.value.id in package
            and id(node) not in annotation_nodes
        ):
            found.append((node.lineno, f"use of the module {CONTEXT_MODULE}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in NEVER_NAMED:
                found.append((node.lineno, f"string {node.value!r}"))
            elif node.value in MODULE_STRINGS:
                found.append((node.lineno, f"string naming the module {CONTEXT_MODULE}"))
    return found


# Expressions a type never needs, refused inside an annotation.
NOT_A_TYPE = (
    ast.Lambda,
    ast.NamedExpr,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Await,
    ast.Yield,
    ast.YieldFrom,
)


def _type_name(node: ast.AST) -> bool:
    """A dotted name with no ``_``-prefixed part: what a generic's subscript is taken of."""
    while isinstance(node, ast.Attribute):
        if node.attr.startswith("_"):
            return False
        node = node.value
    return isinstance(node, ast.Name)


def _as_expression(annotation: ast.AST) -> ast.AST:
    """A whole-string annotation parsed (``typing.get_type_hints`` evaluates it), else itself."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            parsed = ast.parse(annotation.value.strip(), mode="eval").body
        except SyntaxError:
            return annotation
        for sub in ast.walk(parsed):
            ast.copy_location(sub, annotation)
        return parsed
    return annotation


def annotation_findings(
    annotations: list[ast.AST], token_names: set[str], annotated: set[str]
) -> list[tuple[int, str]]:
    """Annotations are evaluated at run time (Python 3.12), so one may hold only a type: a name,
    an attribute chain, a subscript of a type name (a generic), ``|``, literals and tuples or
    lists of these. Refused inside one: a call (except in ``Annotated``'s metadata, where
    FastAPI's ``Depends(...)`` lives), a subscript of anything else, an attribute starting with
    ``_`` (``__globals__``, ``_ISSUER``), the token's names, and lambdas, comprehensions and the
    like. ``annotated`` holds the module's spellings of ``Annotated`` (aliases included)."""
    found: list[tuple[int, str]] = []
    for annotation in annotations:
        root = _as_expression(annotation)
        metadata: set[int] = set()
        for node in ast.walk(root):
            if (
                isinstance(node, ast.Subscript)
                and _type_name(node.value)
                and (_dotted_tail(node.value) == "Annotated" or _dotted(node.value) in annotated)
                and isinstance(node.slice, ast.Tuple)
            ):
                metadata |= {id(n) for e in node.slice.elts[1:] for n in ast.walk(e)}
        for node in ast.walk(root):
            why = None
            if isinstance(node, ast.Call) and id(node) not in metadata:
                why = "a call inside an annotation"
            elif isinstance(node, ast.Subscript) and not _type_name(node.value):
                why = "a subscript of a non-type inside an annotation"
            elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                why = f"use of {node.attr} inside an annotation"
            elif isinstance(node, NOT_A_TYPE):
                why = "an expression that is not a type inside an annotation"
            elif (isinstance(node, ast.Name) and node.id in token_names) or (
                isinstance(node, ast.Attribute) and node.attr in token_names
            ):
                why = "reference to the issuer token inside an annotation"
            elif isinstance(node, ast.Constant) and node.value in token_names:
                why = "string naming the issuer token inside an annotation"
            if why is not None:
                found.append((getattr(node, "lineno", 0), why))
    return found


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([getattr(node, "id", ""), *reversed(parts)])


def _dotted_tail(node: ast.AST) -> str:
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
