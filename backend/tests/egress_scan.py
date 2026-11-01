"""Guard of PLAN §2 and roadmap Amendment 1: only ``ownvitals.egress`` reaches the network (the
package and its submodules; nothing outside it), and ``ownvitals.mail`` only through two
``smtplib`` clients.

It is **closed-world**. Which modules can reach the network is computed from the locked
environment (``egress_closure``), and every one ``src/`` uses must be classified in one reviewed
table (``egress_classified``). ``scan_source(source, module)`` returns what ``module`` (a dotted
name under ``ownvitals``) may not do outside the egress package:

- **import or reference** a network-capable module that is not classified, or denied, or a name
  outside an allowed module's names. A reference is resolved through imports, aliases and chains:
  a chain through any module that bound a module by an import (``asyncio.base_events.socket``,
  ``typing_extensions._socket``, ``ownvitals.mail.smtplib``), and through any ``ownvitals``
  module's re-exports, is judged where it lands. ``getattr`` with a constant name counts;
- import a module **not in the locked environment**, import dynamically (``import_module`` or
  ``__import__`` with a name that is not a constant, a ``package`` or a ``fromlist``), look a
  module up by name (``sys.modules``, ``pkgutil.resolve_name``), use ``ctypes`` or a process
  route the hook cannot see (``PROCESS_MODULES``) by any spelling, or star-import;
- name a classified-out dotted path in a **string** (``"logging.handlers.SysLogHandler"`` for
  ``dictConfig``, ``"handlers.SysLogHandler"`` for ``fileConfig``, ``"ext://socket.socket"``);
- use an ``asyncio`` event-loop network method on any object (``.create_connection``,
  ``.sock_connect`` ...: every method of ``AbstractEventLoop`` but ``LOOP_METHODS``);
- build an allowlist, a transport or an opener of the package (``Allowlist``, ...), hand an
  entry point a list, use a **private name** of ``ownvitals.egress`` or ``ownvitals.mail``, or
  **assign** to (or delete) any attribute of either from outside;
- write to a name or attribute ``TYPE_CHECKING`` (``egress_typeonly``);
- use ``install_worker_allowlist``, which makes a process the worker: only the worker's entry
  point and the probe CLI may (``WORKER_ROLE_MODULES``), and tests.

Type-only imports pass when their names appear only in annotations (``egress_typeonly``). The
rest of deliberate indirection (``vars(sys)``, a computed ``getattr``, ``exec``) is review's (N9).

A worker module never imports ``ownvitals.mail`` (``egress_worker``: ``WORKER_PACKAGES``,
``WORKER_ENTRYPOINTS``, checked by the tests), and every web-only site calls
``refuse_in_worker`` itself (``test_egress_runtime.py``).
"""

import ast
import builtins
import re
from functools import cache
from pathlib import Path

from egress_classified import (
    EDGE_CUTS,
    LOOP_METHODS,
    NATIVE_MODULES,
    PROCESS_MODULES,
    PROCESS_NAMES,
    REFLECTION_NAMES,
    TABLE,
    Entry,
)
from egress_closure import OWN, Closure, resolve_from, under
from egress_typeonly import annotations, assignments, type_only
from egress_typeonly import dotted as _dotted

EGRESS = "ownvitals.egress"
MAIL = "ownvitals.mail"
SEALED = (EGRESS, MAIL)  # no other module uses their private names or assigns to them
_EGRESS_DIR = Path(__file__).resolve().parent.parent / "src" / "ownvitals" / "egress"
# The package's submodules: ``ownvitals.egress.core.Allowlist`` is ``ownvitals.egress.Allowlist``.
EGRESS_SUBMODULES = frozenset(p.stem for p in _EGRESS_DIR.glob("*.py") if p.stem != "__init__")
CLOSURE = Closure(cuts=EDGE_CUTS)  # the run's closure: parses and capability are cached

# The lists and what applies them are built inside the package only (a call or a subclass).
_EGRESS_CONSTRUCTORS = frozenset(
    f"{EGRESS}.{n}"
    for n in (
        "Allowlist",
        "AllowlistTransport",
        "build_allowlist_opener",
        "DeadlineHTTPSHandler",
        "DeadlineHTTPSConnection",
    )
)
# The package's entry points: called only without an allowlist (each list is its own).
_NO_ALLOWLIST_ARGUMENT = frozenset(
    f"{EGRESS}.{n}"
    for n in (
        "withings_async_client",
        "model_async_client",
        "google_async_client",
        "google_oauth_client",
        "exchange_withings_code",
    )
)
INSTALL_WORKER = f"{EGRESS}.install_worker_allowlist"
# The modules that may make their process the worker. Neither exists yet: the names are the ones
# their issues give.
WORKER_ROLE_MODULES: dict[str, str] = {
    "ownvitals.worker.__main__": "the worker process's entry point (#9, not written yet)",
    "ownvitals.withings.probe": "the probe CLI, a worker-side one-off (#29, not written yet)",
}
# The runtime egress hook (ADR 0009): installed only by the process entry points (web's: #77), and
# the SMTP session's connection scope opened only by the mail module.
INSTALL_HOOK = f"{EGRESS}.install_egress_hook"
HOOK_INSTALLERS = frozenset({"ownvitals.worker.__main__", "ownvitals.cli", "ownvitals.web"})
SMTP_SESSION = f"{EGRESS}.smtp_session"
_DOTTED = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+")
_EXCEPTIONS = frozenset(
    n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, BaseException)
)


def in_egress(module: str) -> bool:
    return under(module, (EGRESS,))


# --- the table --------------------------------------------------------------------------------


def lookup(module: str, scanning: str) -> tuple[str, Entry] | None:
    """The entry that classifies ``module`` for code in ``scanning``: its own key, else the
    longest ``prefix.*``; an entry limited to other modules does not apply."""
    keys = [module] + [
        k
        for k in sorted(TABLE, key=len, reverse=True)
        if k.endswith(".*") and under(module, (k[:-2],))
    ]
    for key in keys:
        entry = TABLE.get(key)
        if entry is not None:
            return None if entry.only_in and scanning not in entry.only_in else (key, entry)
    return None


@cache
def exception_classes(module: str) -> frozenset[str]:
    """The classes ``module`` defines that derive from a built-in exception."""
    facts_source = CLOSURE.find(module)
    if facts_source is None or facts_source.source is None:
        return frozenset()
    bases: dict[str, list[str]] = {}
    for node in ast.parse(facts_source.source.read_bytes()).body:
        if isinstance(node, ast.ClassDef):
            bases[node.name] = [b.id for b in node.bases if isinstance(b, ast.Name)]
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, parents in bases.items():
            if name not in found and any(p in _EXCEPTIONS or p in found for p in parents):
                found.add(name)
                changed = True
    return frozenset(found)


def allowed_names(module: str, entry: Entry) -> frozenset[str]:
    return entry.names | (exception_classes(module) if entry.exceptions else frozenset())


def _functions(body: list[ast.stmt]) -> list[str]:
    """The functions defined at this level of ``body`` (``if`` blocks included, not classes)."""
    found: list[str] = []
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(node.name)
        elif isinstance(node, ast.If):
            found += _functions(node.body + node.orelse)
    return found


@cache
def object_network_names() -> frozenset[str]:
    """Names refused as an attribute of any object, read from the locked stdlib: the methods of
    ``asyncio.AbstractEventLoop`` outside ``LOOP_METHODS`` and the functions of
    ``asyncio.streams`` (``open_connection``, ``start_server`` ...). A loop object, or a module
    held in a variable, cannot be told apart statically."""
    names: set[str] = set()
    for module, owner in (("asyncio.events", "AbstractEventLoop"), ("asyncio.streams", None)):
        found = CLOSURE.find(module)
        assert found is not None, module
        assert found.source is not None, module
        body = ast.parse(found.source.read_bytes()).body
        if owner is not None:
            body = next(c.body for c in body if isinstance(c, ast.ClassDef) and c.name == owner)
        names |= {n for n in _functions(body) if not n.startswith("_")}
    return frozenset(names - LOOP_METHODS)


# --- resolving a reference ------------------------------------------------------------------


def visits(dotted: str, depth: int = 0) -> list[tuple[str, tuple[str, ...]]]:
    """``(module, the rest of the chain)`` for each module a reference reaches: the longest
    module prefix, then wherever a name of it leads when the module bound that name by an import
    (always for a bound module; for a bound name only in an ``ownvitals`` module)."""
    parts = dotted.split(".")
    if not CLOSURE.is_module(parts[0]):
        return []
    i = 1
    while i < len(parts) and CLOSURE.is_module(".".join(parts[: i + 1])):
        i += 1
    module, rest = ".".join(parts[:i]), tuple(parts[i:])
    found = [(module, rest)]
    target = CLOSURE.binding(module, rest[0]) if rest and depth < 16 else None
    if target is not None and (CLOSURE.is_module(target) or under(module, (OWN,))):
        found += visits(".".join([target, *rest[1:]]), depth + 1)
    return found


def importable(name: str) -> bool:
    """A module, or a module its parent binds by an import (``os.path``)."""
    if CLOSURE.is_module(name):
        return True
    parent, _, last = name.rpartition(".")
    target = CLOSURE.binding(parent, last) if parent and importable(parent) else None
    return target is not None and CLOSURE.is_module(target)


def classify(dotted: str, scanning: str, verb: str, used: set[str] | None = None) -> list[str]:
    """What is wrong with ``scanning`` importing or referencing ``dotted``."""
    for module, rest in visits(dotted):
        if under(module, (OWN,)) or not CLOSURE.capable(module):
            continue
        hit = lookup(module, scanning)
        if hit is None:
            trail = " -> ".join(CLOSURE.path(module))
            return [f"{verb} {dotted}: {module} can reach the network ({trail}); not classified"]
        key, entry = hit
        if used is not None:
            used.add(key)
        if entry.kind == "denied":
            return [f"{verb} {dotted}: {module} is denied ({entry.reason})"]
        if entry.kind == "allowed" and rest:
            if rest[0] not in allowed_names(module, entry):
                return [f"{verb} {dotted}: {rest[0]} is not an allowed name of {module}"]
            if used is not None:
                used.add(f"{key}:{rest[0]}")
    return []


# --- aliases ------------------------------------------------------------------------------------


def _is_package(module: str) -> bool:
    found = CLOSURE.find(module)
    return found is not None and found.search is not None


def _aliases(tree: ast.AST, module: str, skip: set[int] | None = None) -> dict[str, str]:
    """Local name -> the dotted name it stands for, from every import in the file but ``skip``."""
    names: dict[str, str] = {}
    package = _is_package(module)
    for node in ast.walk(tree):
        if skip and id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                names[alias.asname or top] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom):
            source = resolve_from(node, module, package)
            for alias in node.names:
                names[alias.asname or alias.name] = f"{source}.{alias.name}"
    return names


def _canonical(name: str) -> str:
    """The egress package's re-exports of its submodules: ``ownvitals.egress.core.X`` is
    ``ownvitals.egress.X``."""
    prefix = EGRESS + "."
    if name.startswith(prefix):
        head, _, rest = name[len(prefix) :].partition(".")
        if head in EGRESS_SUBMODULES and rest:
            return prefix + rest
    return name


# --- the rules ----------------------------------------------------------------------------------


def _private_sealed(name: str, module: str) -> bool:
    for sealed in SEALED:
        if under(name, (sealed,)) and not under(module, (sealed,)):
            parts = name[len(sealed) + 1 :].split(".") if name != sealed else []
            if any(
                p.startswith("_") and not (p.startswith("__") and p.endswith("__")) for p in parts
            ):
                return True
    return False


def _internal(name: str, module: str, verb: str) -> list[str]:
    name = _canonical(name)
    found: list[str] = []
    if _private_sealed(name, module):
        found.append(f"{verb} {name}: private to its module")
    if name == INSTALL_WORKER and module not in WORKER_ROLE_MODULES:
        found.append(f"{verb} {name}: only the worker's entry point makes a process the worker")
    if name == INSTALL_HOOK and module not in HOOK_INSTALLERS:
        found.append(f"{verb} {name}: only a process entry point installs the egress hook")
    if name == SMTP_SESSION and module != MAIL:
        found.append(f"{verb} {name}: only the mail module opens an SMTP session")
    spelled = _landings(name)
    if any(under(n, PROCESS_MODULES) or n in PROCESS_NAMES for n in spelled):
        found.append(f"{verb} {name}: starts a process the egress hook cannot see (ADR 0009)")
    if any(under(n, NATIVE_MODULES) for n in spelled):
        found.append(f"{verb} {name}: calls native code the egress hook cannot see (ADR 0009)")
    if any(under(n, REFLECTION_NAMES) for n in spelled):
        found.append(f"{verb} {name}: resolves a module by name at run time")
    return found


@cache
def _landings(name: str) -> frozenset[str]:
    """``name`` and where import bindings take it (``os.sys.modules`` is ``sys.modules``)."""
    return frozenset({name, *(".".join([m, *rest]) for m, rest in visits(name))})


def _imports(tree: ast.AST, module: str, skip: set[int]) -> list[tuple[int, str, bool]]:
    """``(line, imported name, must be a module)`` for every run-time import."""
    found: list[tuple[int, str, bool]] = []
    package = _is_package(module)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name, True) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            source = resolve_from(node, module, package)
            found.append((node.lineno, source, True))
            found += [(node.lineno, f"{source}.{a.name}", False) for a in node.names]
    return found


def _dynamic_import(node: ast.Call, aliases: dict[str, str]) -> list[tuple[int, str, bool]] | str:
    """A constant ``import_module``/``__import__`` as an import, or why it is refused."""
    func = node.func
    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if called not in ("import_module", "__import__"):
        return []
    first = node.args[0] if node.args else None
    extra = node.args[1:] or [k for k in node.keywords if k.arg in ("package", "fromlist", None)]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)) or extra:
        return f"imports dynamically ({called}): a module name must be one constant"
    return [(node.lineno, first.value, True)]


def _strings(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.removeprefix("ext://")
            if _DOTTED.fullmatch(text) and not under(text, (OWN,)):
                found.append((node.lineno, text))
                if text.startswith("handlers."):  # fileConfig resolves it in ``logging``
                    found.append((node.lineno, f"logging.{text}"))
    return found


def scan_source(source: str, module: str, used: set[str] | None = None) -> list[tuple[int, str]]:
    """``(line, why)`` for everything ``module`` does outside the outbound allowlist; ``used``
    collects the table entries and names it relied on."""
    if in_egress(module):
        return []
    tree = ast.parse(source)
    every = _aliases(tree, module)
    skip = type_only(tree, source)
    # In an annotation a name means what the run-time imports bind; anywhere else a name bound
    # only for type checking still counts (it could only run if rebound).
    typed = annotations(tree)
    runtime = _aliases(tree, module, skip)
    aliases = every | runtime
    found: list[tuple[int, str]] = assignments(tree)
    imports = _imports(tree, module, skip)
    object_names = object_network_names()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            found.append((node.lineno, "star import: names must be visible"))
        elif isinstance(node, ast.Call):
            dynamic = _dynamic_import(node, aliases)
            if isinstance(dynamic, str):
                found.append((node.lineno, dynamic))
            else:
                imports += dynamic
            found += [(node.lineno, w) for w in _scan_call(node, aliases, module, used)]
    for line, name, must_be_module in imports:
        found += [(line, w) for w in _internal(name, module, "imports")]
        head = name.split(".")[0]
        if not CLOSURE.is_module(head) or (must_be_module and not importable(name)):
            found.append((line, f"imports {name}: not in the locked environment"))
            continue
        found += [(line, w) for w in classify(name, module, "imports", used)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in object_names:
            found.append((node.lineno, f"uses .{node.attr}: an asyncio network method"))
        if isinstance(node, ast.Attribute | ast.Name):
            name = _dotted(node, runtime if id(node) in typed else aliases)
            if name is None:
                continue
            found += [(node.lineno, w) for w in _internal(name, module, "uses")]
            found += [(node.lineno, w) for w in classify(name, module, "uses", used)]
            if isinstance(node.ctx, ast.Store | ast.Del) and isinstance(node, ast.Attribute):
                found += [(node.lineno, w) for w in _assigned(name, module)]
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = _dotted(base, aliases)
                if name is not None and _canonical(name) in _EGRESS_CONSTRUCTORS:
                    found.append((node.lineno, f"subclasses {name}: lists live in {EGRESS}"))
    for line, text in _strings(tree):
        found += [(line, w) for w in classify(text, module, "names", used)]
    return sorted(set(found))


def _assigned(name: str, module: str) -> list[str]:
    if any(under(name, (s,)) and not under(module, (s,)) for s in SEALED):
        return [f"assigns {name}: the egress package and mail are not patched from outside"]
    return []


def _scan_call(
    node: ast.Call, aliases: dict[str, str], module: str, used: set[str] | None
) -> list[str]:
    func = node.func
    called = getattr(func, "id", None)
    if called in ("getattr", "setattr", "delattr") and len(node.args) >= 2:
        target, attr = _dotted(node.args[0], aliases), node.args[1]
        if target is not None and isinstance(attr, ast.Constant) and isinstance(attr.value, str):
            name = f"{target}.{attr.value}"
            if called != "getattr":
                return _assigned(name, module)
            return _internal(name, module, "uses") + classify(name, module, "uses", used)
        if target is not None and called != "getattr":
            return _assigned(target + ".?", module)
    name = _dotted(func, aliases)
    if name is None:
        return []
    name = _canonical(name)
    widened = node.args or any(k.arg in (None, "allowlist") for k in node.keywords)
    if name in _NO_ALLOWLIST_ARGUMENT and widened:
        return [f"hands {name} an allowlist: lists live in {EGRESS}"]
    if name in _EGRESS_CONSTRUCTORS:
        return [f"calls {name}: lists live in {EGRESS}"]
    return []


def module_name(path: Path, src: Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def sources(src: Path) -> list[tuple[str, str]]:
    """``(module, source)`` of every module of the ``ownvitals`` package under ``src``."""
    return [
        (module_name(p, src), p.read_text(encoding="utf-8"))
        for p in sorted((src / "ownvitals").rglob("*.py"))
    ]


def scan_tree(src: Path, used: set[str] | None = None) -> list[tuple[str, int, str]]:
    """Every violation in the ``ownvitals`` package under ``src``."""
    return [
        (module, line, why)
        for module, source in sources(src)
        for line, why in scan_source(source, module, used)
    ]


def imported_modules(source: str, module: str) -> set[str]:
    """Every dotted name ``module`` imports at run time (and each ``from`` source)."""
    tree = ast.parse(source)
    skip = type_only(tree, source)
    return {name for _, name, _ in _imports(tree, module, skip)}
