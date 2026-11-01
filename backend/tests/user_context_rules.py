"""Guard 5's rules for the issuer token's other routes (#89), used by ``user_context_scan``.

``findings(tree)`` refuses, in every scanned module (the issuer modules are not scanned), type
annotations included:

- the attribute, name or string ``__globals__``, ``__kwdefaults__`` and ``__defaults__``: a
  function's globals hold the issuer token (``require_context.__globals__``), and a default can
  pass it;
- ``import_module`` (any module's, under any import alias) other than called directly with a
  string literal (``load = importlib.import_module`` included), and ``sys.modules`` looked up
  by a computed key (``sys.modules[name]``, ``sys.modules.get(name)``): a module name built at
  run time (``repr(type(ctx))`` sliced) turns into the module that holds the token. A literal
  is judged by the string rules (``"ownvitals.context"`` is refused). ``__module__`` and
  ``__import__`` are dunders, refused by ``user_context_dunders``;
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
- ``inspect``, ``pydoc``, ``linecache`` and ``tokenize`` as an import (any alias or ``from``
  form), a name, an attribute or a string (``importlib.import_module("inspect")``): they turn a
  docstring, a comment or a line of source into a value (``pkgutil.resolve_name(
  inspect.getdoc(h))``, ``linecache.getline(path, 3)``). ``importlib.util`` likewise
  (``find_spec(...).origin`` finds a module's file): refused except ``import importlib.util``
  and the one call ``importlib.util.find_spec('uvloop')`` in ``egress/hook.py``;
- the module ``ownvitals.context`` reached from its package (the twelfth #115 review):
  - the package name (``import ownvitals``, or the root bound by ``import ownvitals.db``) loaded
    other than as the root of an attribute chain whose next part isn't ``context``: bound to a
    name (``o = ownvitals``), passed (``vars(ownvitals)``, ``dir``, ``getattr``), subscripted;
  - the attribute ``context`` on anything but ``self`` (``import_module(".", "ownvitals")
    .context``, ``p.context``), outside type annotations;
  - any string, docstrings and comments aside, with ``context`` as an identifier segment
    (``attrgetter("context")``, ``getattr(o, "context")``, ``k == "context"``,
    ``"..context"``);
  - the bare ``"ownvitals"`` as any argument of a lookup (``import_module``, ``resolve_name``,
    ``locate``, ``find_spec``, ``.get``/``.pop``/``.setdefault``, ``package=`` included) or a
    subscript key (``sys.modules["ownvitals"]``);
  - ``import_module`` with a relative name (a leading ``.``), even a literal one;
  - a ``str`` or ``bytes`` constant exactly ``"ownvitals"`` (f-string parts included), outside
    its pinned positions (``PACKAGE_STRING_PINS``, each once in its module): the package's name
    is never written as a string (the fourteenth review). ``"ownvitals.web"`` is another string;
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

from user_context_dunders import SEGMENT, binds, own_nodes, strings

CONTEXT_MODULE = "ownvitals.context"
PACKAGE = "ownvitals"
# Never named: ``__globals__`` leads to the token; ``__kwdefaults__`` and
# ``__defaults__`` would make it a default (``K.__init__.__kwdefaults__["issuer"] = tok``);
# ``copyreg`` builds a blank instance without calling ``__init__``.
# ``inspect`` and ``pydoc`` read docstrings and comments back as values.
SOURCE_READERS = {"inspect", "pydoc", "linecache", "tokenize"}
NEVER_NAMED = {"__globals__", "__kwdefaults__", "__defaults__", "copyreg", *SOURCE_READERS}
# ``importlib.util``: only ``import importlib.util`` and this one call, in ``egress/hook.py``.
IMPORTLIB_UTIL = "importlib.util"
HOOK = "ownvitals/egress/hook.py"
UVLOOP_CHECK = "importlib.util.find_spec('uvloop')"
# Calls that look a module up by its name (and ``sys.modules``' own lookups).
PACKAGE_LOOKUPS = {
    "import_module",
    "resolve_name",
    "locate",
    "find_spec",
    "get",
    "pop",
    "setdefault",
}
# The attribute name that reaches the context module from its package.
CONTEXT_ATTRIBUTE = "context"
# ``self.context.wrap_socket(...)`` is allowed here only (``_wrap_socket_contexts``).
MAIL = "ownvitals/mail.py"
# ``"ownvitals"`` as a string: only in these positions, each once in its module.
VERSION_ARGUMENT = "the argument of version(...)"
PACKAGE_STRING_PINS = {
    "ownvitals/__init__.py": VERSION_ARGUMENT,
    "ownvitals/auth/totp.py": "the value of ISSUER = ...",
    "ownvitals/egress/code_exchange.py": "the value of a 'User-Agent' key",
    "ownvitals/web.py": "the keyword title=",
}
# The issuer token's names (also in ``user_context_scan``).
TOKEN_NAMES = {"issuer_token", "_ISSUER"}
# ``sys.modules`` methods that take a module name.
MODULES_LOOKUPS = {"get", "pop", "setdefault"}
# ``importlib.import_module`` under any spelling: only with a literal module name.
IMPORT_MODULE = "import_module"
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
            if any(_within(alias.name, module) for module in SOURCE_READERS):
                return f"import of {alias.name}"
            if _within(alias.name, CONTEXT_MODULE) and alias.asname:
                return f"import of the module {CONTEXT_MODULE}"
        return None
    if _within(node.module, "copyreg"):
        return "import of copyreg"
    if any(_within(node.module, module) for module in SOURCE_READERS):
        return f"import from {node.module}"
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


def _import_module_names(tree: ast.AST) -> set[str]:
    """``import_module`` and every local name an import binds to it."""
    return {IMPORT_MODULE} | {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == IMPORT_MODULE
    }


def _literal_imports(tree: ast.AST, names: set[str]) -> set[int]:
    """Ids of the ``import_module`` references called directly with a literal module name, by
    position or as ``name=``."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == IMPORT_MODULE) or (
            isinstance(func, ast.Name) and func.id in names
        ):
            given = node.args[0] if node.args else None
            given = next((k.value for k in node.keywords if k.arg == "name"), given)
            if not _computed(given) and not str(getattr(given, "value", "")).startswith("."):
                ids.add(id(func))  # a literal, absolute module name
    return ids


def _importlib_util_findings(tree: ast.AST, module: str | None) -> list[tuple[int, str]]:
    """``importlib.util`` outside its one use (see ``IMPORTLIB_UTIL``)."""
    found: list[tuple[int, str]] = []
    importlib_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib" or (alias.name == IMPORTLIB_UTIL and not alias.asname):
                    importlib_names.add(alias.asname or "importlib")
                if _within(alias.name, IMPORTLIB_UTIL) and (alias.asname or module != HOOK):
                    found.append((node.lineno, f"import of {IMPORTLIB_UTIL}"))
        elif isinstance(node, ast.ImportFrom) and (
            _within(node.module, IMPORTLIB_UTIL)
            or (node.module == "importlib" and any(a.name in {"util", "*"} for a in node.names))
        ):
            found.append((node.lineno, f"import from {IMPORTLIB_UTIL}"))
    allowed: set[int] = set()
    if module == HOOK:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node) == UVLOOP_CHECK:
                assert isinstance(node.func, ast.Attribute)
                allowed.add(id(node.func.value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "util"
            and isinstance(node.value, ast.Name)
            and node.value.id in importlib_names
            and id(node) not in allowed
        ):
            found.append((node.lineno, f"use of {IMPORTLIB_UTIL}"))
    return found


def _is_string(node: ast.AST | None, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _wrap_socket_contexts(tree: ast.AST, module: str | None) -> set[int]:
    """Ids of the ``self.context`` in ``self.context.wrap_socket(...)``, in ``mail.py`` only,
    where ``self`` is the first parameter of a method in a class body, never rebound there (nor
    by ``nonlocal``/``global``), and the call is in the method's own body."""
    allowed: set[int] = set()
    if module != MAIL:
        return allowed
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            positional = [*method.args.posonlyargs, *method.args.args]
            if not positional or positional[0].arg != "self" or method.decorator_list:
                continue
            nodes = own_nodes(method)
            if any(binds(node, "self") for node in nodes):
                continue
            if any(
                isinstance(node, ast.Nonlocal | ast.Global) and "self" in node.names
                for node in ast.walk(method)
            ):
                continue
            for node in nodes:
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "wrap_socket"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == CONTEXT_ATTRIBUTE
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "self"
                ):
                    allowed.add(id(node.func.value))
    return allowed


def _pinned_package_string(tree: ast.AST, module: str | None) -> set[int]:
    """The id of the one ``"ownvitals"`` in its module's pinned position, if exactly one is."""
    pin = PACKAGE_STRING_PINS.get(module or "")
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and pin == VERSION_ARGUMENT:
            if isinstance(node.func, ast.Name) and node.func.id == "version" and not node.keywords:
                found += node.args[:1] if len(node.args) == 1 else []
        elif isinstance(node, ast.Call) and module == "ownvitals/web.py":
            found += [k.value for k in node.keywords if k.arg == "title"]
        elif isinstance(node, ast.Assign) and module == "ownvitals/auth/totp.py":
            if [ast.unparse(t) for t in node.targets] == ["ISSUER"]:
                found.append(node.value)
        elif isinstance(node, ast.Dict) and module == "ownvitals/egress/code_exchange.py":
            found += [
                v
                for k, v in zip(node.keys, node.values, strict=True)
                if _is_string(k, "User-Agent")
            ]
    hits = [node for node in found if _is_string(node, PACKAGE)]
    return {id(hits[0])} if len(hits) == 1 else set()


def _public_continuations(tree: ast.AST) -> set[int]:
    """Ids of the attributes ``x.context`` that continue to a public name
    (``ownvitals.context.UserContext``): the one form an annotation may use."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and not node.attr.startswith("_")
        and node.attr not in TOKEN_NAMES
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == CONTEXT_ATTRIBUTE
    }


def _package_findings(
    tree: ast.AST, package: set[str], annotation_nodes: set[int], module: str | None
) -> list[tuple[int, str]]:
    """The context module reached from its package (see the module docstring). ``package``
    holds the local names bound to the package. In an annotation (a ``TypeAlias`` value
    included) ``ownvitals.context`` may only continue to a public name
    (``ownvitals.context.UserContext``); ``self.context`` is pinned to ``mail.py``'s
    ``wrap_socket`` (``_wrap_socket_contexts``)."""
    found: list[tuple[int, str]] = []
    chain_roots = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and (node.attr != CONTEXT_ATTRIBUTE or id(node) in annotation_nodes)
    }
    continued = _public_continuations(tree)
    wrap_socket = _wrap_socket_contexts(tree, module)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        annotation = id(node) in annotation_nodes
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in package
            and id(node) not in chain_roots
        ):
            found.append((line, "the package ownvitals as a value"))
        elif isinstance(node, ast.Attribute) and node.attr == CONTEXT_ATTRIBUTE:
            if annotation and id(node) not in continued:
                found.append((line, "ownvitals.context in an annotation, not a name in it"))
            elif not annotation and id(node) not in wrap_socket:
                found.append((line, "the attribute context"))
        elif isinstance(node, ast.Subscript) and _is_string(node.slice, PACKAGE):
            found.append((line, "the package looked up by its name"))
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            given = [*node.args, *(k.value for k in node.keywords)]
            if called in PACKAGE_LOOKUPS and any(_is_string(arg, PACKAGE) for arg in given):
                found.append((line, f"{called} of the package"))
    pinned = _pinned_package_string(tree, module)
    for node in ast.walk(tree):
        value = node.value if isinstance(node, ast.Constant) else None
        if value in (PACKAGE, PACKAGE.encode()) and id(node) not in pinned:
            found.append((getattr(node, "lineno", 0), "the package's name as a string"))
    for node, text in strings(tree):  # docstrings and comments aside
        if CONTEXT_ATTRIBUTE in SEGMENT.findall(text):
            found.append((getattr(node, "lineno", 0), "a string naming context"))
    return found


def findings(
    tree: ast.AST, annotation_nodes: set[int], module: str | None = None
) -> list[tuple[int, str]]:
    """What these rules refuse in ``tree``, annotations included (they run on 3.12).
    ``annotation_nodes`` are the ids of the nodes inside type annotations, where the package
    attribute ``ownvitals.context`` may name a type (``ownvitals.context.UserContext``).
    ``module`` is the path under ``src`` (``importlib.util``'s one use is in ``hook.py``)."""
    found: list[tuple[int, str]] = [
        *_importlib_util_findings(tree, module),
        *_package_findings(tree, _package_names(tree), annotation_nodes, module),
    ]
    package = _package_names(tree)
    builtins = _builtins_names(tree)
    modules = _sys_modules_names(tree)
    loaders = _import_module_names(tree)
    literal_imports = _literal_imports(tree, loaders)
    for node in ast.walk(tree):
        if (
            (isinstance(node, ast.Attribute) and node.attr == IMPORT_MODULE)
            or (isinstance(node, ast.Name) and node.id in loaders)
        ) and id(node) not in literal_imports:
            found.append((node.lineno, "import_module other than called with a literal"))
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
