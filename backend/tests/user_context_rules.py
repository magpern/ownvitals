"""Guard 5's rules for the issuer token's other routes (#89), used by ``user_context_scan``.

``findings(tree)`` refuses, in every scanned module (the issuer modules are not scanned):

- the attribute, name or string ``__globals__`` and ``__module__``: a function's globals hold
  the issuer token (``require_context.__globals__``), and ``sys.modules[f.__module__]`` is the
  module that holds it;
- the module object ``ownvitals.context``: every import that binds it (``import
  ownvitals.context as c``, ``from ownvitals import context``, a relative ``from .. import
  context``), ``ownvitals.context`` reached as an attribute of the package outside a type
  annotation, and the strings ``"ownvitals.context"`` and ``".context"`` (``sys.modules[...]``,
  ``sys.modules.get(...)``, ``importlib.import_module``, ``__import__``). With the module,
  ``vars(c)`` or ``c.__dict__`` hold the token. A plain ``import ownvitals.context`` binds only
  the package, like ``import ownvitals.db``, so it stays allowed for annotations and its use is
  judged instead. Importing names from the module (``from ownvitals.context import
  require_context``) stays allowed: the scan judges each name;
- an ``issuer=`` keyword in any call, and a ``**`` argument that spells ``"issuer"`` anywhere in
  it (``**{"issuer": t}``, ``**{k: t for k in ["issuer"]}``, ``**dict([("issuer", t)])``,
  ``**({} | {"issuer": t})``): only the issuer modules pass the token;
- ``copyreg`` as an import, a name, an attribute or a string: ``copyreg.__newobj__(K)`` makes a
  blank context without the token.

What a static scan can't see stays with the review gate (ADR 0002 section 1), and
``docs/dev/isolation.md`` lists it: names built at run time, a keyword dict bound to a name
first (``kw = {"issuer": t}`` then ``K(..., **kw)``), the module found by a computed name.
"""

import ast

CONTEXT_MODULE = "ownvitals.context"
PACKAGE = "ownvitals"
# Never named: ``__globals__`` and ``__module__`` lead to the token; ``copyreg`` builds a blank
# instance without calling ``__init__``.
NEVER_NAMED = {"__globals__", "__module__", "copyreg"}
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
    """What these rules refuse in ``tree``; ``annotation_nodes`` are the ids of the nodes inside
    type annotations, where the module may be named."""
    found: list[tuple[int, str]] = []
    package = _package_names(tree)
    for node in ast.walk(tree):
        if id(node) in annotation_nodes:
            continue
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
        ):
            found.append((node.lineno, f"use of the module {CONTEXT_MODULE}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in NEVER_NAMED:
                found.append((node.lineno, f"string {node.value!r}"))
            elif node.value in MODULE_STRINGS:
                found.append((node.lineno, f"string naming the module {CONTEXT_MODULE}"))
    return found
