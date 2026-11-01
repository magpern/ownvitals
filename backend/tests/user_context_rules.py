"""Guard 5's rules for the issuer token's other routes (#89), used by ``user_context_scan``.

``findings(tree)`` refuses, in every scanned module (the issuer modules are not scanned), type
annotations included:

- the attribute, name or string ``__globals__``, ``__kwdefaults__`` and ``__defaults__``: a
  function's globals hold the issuer token (``require_context.__globals__``), and a default can
  pass it;
- ``sys.modules`` looked up by a computed key (``sys.modules[f.__module__]``,
  ``sys.modules.get(name)``): ``f.__module__`` names the module that holds the token. A literal
  key is judged by the string rules (``"ownvitals.context"`` is refused). ``__module__`` itself
  stays allowed: ``egress.hook`` compares an event loop's module by it;
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

- ``eval``, ``exec`` and ``compile``, the builtins: as a name, imported from ``builtins`` under
  any alias, or as an attribute of ``builtins`` or ``__builtins__``. Code in a string runs
  unseen by the scan. ``re.compile`` and other modules' functions of those names stay allowed.

Annotations follow the type grammar of ``user_context_types``.

Threat model (``docs/dev/isolation.md``): these rules stop plausible, review-visible forgeries;
they are not a sandbox. Reflection in the process (the package object, ``sys.modules``, ``gc``,
``inspect``, ``ctypes``), names built at run time and a keyword dict bound to a name first stay
with code review, RLS and the role checks (ADR 0002 section 1).
"""

import ast

CONTEXT_MODULE = "ownvitals.context"
PACKAGE = "ownvitals"
# Never named: ``__globals__`` leads to the token; ``__kwdefaults__`` and
# ``__defaults__`` would make it a default (``K.__init__.__kwdefaults__["issuer"] = tok``);
# ``copyreg`` builds a blank instance without calling ``__init__``.
NEVER_NAMED = {"__globals__", "__kwdefaults__", "__defaults__", "copyreg"}
# ``sys.modules`` methods that take a module name.
MODULES_LOOKUPS = {"get", "pop", "setdefault"}
# Builtins that run code given as a string.
CODE_BUILTINS = {"eval", "exec", "compile"}
BUILTINS_MODULES = {"builtins", "__builtins__"}
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
    if node.module == "builtins" and any(a.name in CODE_BUILTINS for a in node.names):
        return "import of eval, exec or compile"
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


def _builtins_names(tree: ast.AST) -> set[str]:
    """Local names bound to the ``builtins`` module, plus ``__builtins__``."""
    names = {"__builtins__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.asname or a.name for a in node.names if a.name in BUILTINS_MODULES}
    return names


def _sys_modules_names(tree: ast.AST) -> set[str]:
    """Local spellings of ``sys.modules``: ``sys.modules`` (any alias of ``sys``) and ``modules``
    from ``from sys import modules`` (any alias)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {f"{a.asname or a.name}.modules" for a in node.names if a.name == "sys"}
        elif isinstance(node, ast.ImportFrom) and node.module == "sys":
            names |= {a.asname or a.name for a in node.names if a.name == "modules"}
    return names | {"sys.modules"}


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def _computed(key: ast.AST | None) -> bool:
    return not (isinstance(key, ast.Constant) and isinstance(key.value, str))


def _modules_by_computed_key(node: ast.AST, modules: set[str]) -> bool:
    """``sys.modules[<not a string literal>]`` or ``sys.modules.get(<not a string literal>)``."""
    if isinstance(node, ast.Subscript) and _dotted(node.value) in modules:
        return _computed(node.slice)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in MODULES_LOOKUPS
        and _dotted(node.func.value) in modules
        and _computed(node.args[0] if node.args else None)
    )


def findings(tree: ast.AST, annotation_nodes: set[int]) -> list[tuple[int, str]]:
    """What these rules refuse in ``tree``, annotations included (they run on 3.12).
    ``annotation_nodes`` are the ids of the nodes inside type annotations, where the package
    attribute ``ownvitals.context`` may name a type (``ownvitals.context.UserContext``)."""
    found: list[tuple[int, str]] = []
    package = _package_names(tree)
    builtins = _builtins_names(tree)
    modules = _sys_modules_names(tree)
    for node in ast.walk(tree):
        if _modules_by_computed_key(node, modules):
            found.append((getattr(node, "lineno", 0), "sys.modules looked up by a computed key"))
        if isinstance(node, ast.Name) and node.id in CODE_BUILTINS:
            found.append((node.lineno, f"use of {node.id}"))
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in CODE_BUILTINS
            and isinstance(node.value, ast.Name)
            and node.value.id in builtins
        ):
            found.append((node.lineno, f"use of builtins.{node.attr}"))
        elif isinstance(node, ast.Import | ast.ImportFrom):
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
