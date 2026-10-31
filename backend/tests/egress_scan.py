"""Guard of PLAN §2 and roadmap Amendment 1: only ``ownvitals.egress`` reaches the network (the
package and its submodules; nothing outside it), and ``ownvitals.mail`` only through ``smtplib``.

It **denies by default**. ``scan_source(source, module)`` returns what ``module`` (a dotted name
under ``ownvitals``) may not do outside the egress package:

- **import** a network-capable module, a submodule or a name from one (``NETWORK_MODULES``:
  ``socket``, ``ssl``, ``http.client``, ``urllib.request``, ``smtplib``, ``httpx``, ``requests``,
  ``aiohttp``, ``websockets``, ``multiprocessing.connection``, ``authlib.integrations``,
  ``anthropic``, ``openai``, ``asyncio.streams`` ...), also through ``importlib.import_module`` or
  ``__import__`` with a constant name, or **use** a name under one, also through a module that
  imported it (``ownvitals.egress.clients.httpx.AsyncClient``, ``ownvitals.mail.smtplib.SMTP``).
  ``ALLOWED_IMPORTS`` lists the exceptions, each with its reason: only ``smtplib`` in
  ``ownvitals.mail``, which may still build no client but ``EXEMPT``'s. Imports inside an
  ``if TYPE_CHECKING:`` block are allowed: they do not exist at run time;
- **call** (or import) a connection function of the packages too broad to ban at import
  (``CONNECTION_FUNCTIONS``: ``asyncio``'s ``open_connection``, ``loop.create_connection``,
  ``loop.create_datagram_endpoint``, ``loop.sock_connect`` ..., ``anyio``'s ``connect_tcp``,
  ``connect_unix``, ``create_udp_socket`` ...), on whatever object;
- build an allowlist, a transport or an opener of the package itself (``Allowlist``,
  ``AllowlistTransport``, ``build_allowlist_opener``, the deadline handlers), hand a client
  builder an allowlist, or use a **private name** of ``ownvitals.egress`` or ``ownvitals.mail``
  (``_network_transport``, ``_worker_list``, ``_DeadlineSMTP`` ...);
- use ``install_worker_allowlist``, which makes a process the worker: only the worker's entry
  point and the probe CLI may (``WORKER_ROLE_MODULES``), and tests.

Names are resolved through imports and aliases (``import httpx as h``, ``from urllib import
request``, ``from ownvitals.egress.core import X``, relative imports). Like the other guards this
catches accidental use and makes the rest visible in review; it cannot stop obfuscated code.

A worker module never imports ``ownvitals.mail`` (``WORKER_PACKAGES``, ``WORKER_ENTRYPOINTS``,
checked by the tests), and every web-only site calls ``refuse_in_worker`` itself
(``test_egress_runtime.py``).
"""

import ast
from collections.abc import Iterable
from pathlib import Path

EGRESS = "ownvitals.egress"
MAIL = "ownvitals.mail"
# Modules whose private names no other module may use.
SEALED = (EGRESS, MAIL)
_EGRESS_DIR = Path(__file__).resolve().parent.parent / "src" / "ownvitals" / "egress"
# The package's submodules: ``ownvitals.egress.core.Allowlist`` is ``ownvitals.egress.Allowlist``.
EGRESS_SUBMODULES = frozenset(p.stem for p in _EGRESS_DIR.glob("*.py") if p.stem != "__init__")

# Network-capable modules: outside the egress package nothing imports or uses one.
NETWORK_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "http.client",
        "urllib.request",
        "smtplib",
        "httpx",
        "httpx2",
        "httpcore",
        "requests",
        "aiohttp",
        "urllib3",
        "pycurl",
        "websocket",
        "websockets",
        "multiprocessing.connection",
        "multiprocessing.managers",
        "authlib.integrations",
        "anthropic",
        "openai",
        "asyncio.streams",
        "ftplib",
        "telnetlib",
        "imaplib",
        "poplib",
        "nntplib",
        "smtpd",
        "xmlrpc.client",
    }
)
# module -> network modules it may import and use, with the reason.
ALLOWED_IMPORTS: dict[str, dict[str, str]] = {
    MAIL: {"smtplib": "web only: account mail; the SMTP host is a setting (Amendment 1)"},
}
# module -> the clients it may build from those (a call or a subclass), with the reason.
EXEMPT: dict[str, dict[str, str]] = {
    MAIL: {
        "smtplib.SMTP": "web only: account mail, with its total deadline",
        "smtplib.SMTP_SSL": "web only: account mail over implicit TLS",
    },
}
# The client classes of the allowed modules: a module allowed the module builds only EXEMPT's.
_CLIENT_CLASSES = frozenset({"smtplib.SMTP", "smtplib.SMTP_SSL", "smtplib.LMTP"})
# Connection functions of asyncio and anyio (too broad to ban at import): refused as a call on
# any object, as an import and as a name under either package.
CONNECTION_FUNCTIONS = frozenset(
    {
        "open_connection",
        "open_unix_connection",
        "create_connection",
        "create_unix_connection",
        "create_datagram_endpoint",
        "sock_connect",
        "connect_tcp",
        "connect_unix",
        "create_udp_socket",
        "create_connected_udp_socket",
        "create_unix_datagram_socket",
        "create_connected_unix_datagram_socket",
    }
)
# The lists and what applies them are built inside the package only (a call or a subclass).
_EGRESS_CONSTRUCTORS = frozenset(
    {
        f"{EGRESS}.Allowlist",
        f"{EGRESS}.AllowlistTransport",
        f"{EGRESS}.build_allowlist_opener",
        f"{EGRESS}.DeadlineHTTPSHandler",
        f"{EGRESS}.DeadlineHTTPSConnection",
    }
)
# The package's entry points: called only without an allowlist (each list is its own).
_NO_ALLOWLIST_ARGUMENT = frozenset(
    {
        f"{EGRESS}.withings_async_client",
        f"{EGRESS}.model_async_client",
        f"{EGRESS}.google_async_client",
        f"{EGRESS}.google_oauth_client",
        f"{EGRESS}.exchange_withings_code",
    }
)
INSTALL_WORKER = f"{EGRESS}.install_worker_allowlist"
# The modules that may make their process the worker. Neither exists yet: the names are the ones
# their issues give.
WORKER_ROLE_MODULES: dict[str, str] = {
    "ownvitals.worker.__main__": "the worker process's entry point (#9, not written yet)",
    "ownvitals.withings.probe": "the probe CLI, a worker-side one-off (#29, not written yet)",
}

# Packages that run in the worker: they may never import the web-only mail module.
WORKER_PACKAGES = (
    "ownvitals.worker",
    "ownvitals.jobs",
    "ownvitals.withings",
    "ownvitals.ingest",
    "ownvitals.analysis",
    "ownvitals.ecg",
    "ownvitals.scores",
    "ownvitals.findings",
    "ownvitals.reports",
    "ownvitals.insights",
)
# What the worker process imports at start. Add each new worker entry point here (the worker
# loop, the job runner, the Withings client ...); the reachability test imports them all in a
# fresh interpreter and fails if mail, or a client library they use, is loaded.
WORKER_ENTRYPOINTS = (
    "ownvitals.egress",
    "ownvitals.config",
    "ownvitals.log",
    "ownvitals.db",
    "ownvitals.jobs",
)
# What the worker must not load (the egress package itself runs on ssl, socket, http.client,
# urllib.request, httpx and httpcore).
_REACHABILITY_WATCHED = (
    MAIL,
    "authlib",
    "smtplib",
    "requests",
    "aiohttp",
    "urllib3",
    "pycurl",
    "websocket",
    "websockets",
    "httpx2",
    "openai",
    "ftplib",
    "telnetlib",
    "imaplib",
    "poplib",
    "nntplib",
    "smtpd",
    "xmlrpc.client",
)


def _under(name: str, prefixes: Iterable[str]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def in_egress(module: str) -> bool:
    return _under(module, (EGRESS,))


def _private_sealed(name: str, module: str) -> bool:
    for sealed in SEALED:
        if _under(name, (sealed,)) and not _under(module, (sealed,)):
            parts = name[len(sealed) + 1 :].split(".") if name != sealed else []
            if any(
                p.startswith("_") and not (p.startswith("__") and p.endswith("__")) for p in parts
            ):
                return True
    return False


def _resolve_from(node: ast.ImportFrom, module: str) -> str:
    if not node.level:
        return node.module or ""
    base = module.split(".")[: len(module.split(".")) - node.level]
    return ".".join([*base, node.module] if node.module else base)


def _type_only(tree: ast.AST, aliases: dict[str, str]) -> set[int]:
    """``id``s of the import nodes inside ``if TYPE_CHECKING:`` (``typing``'s, by any alias);
    the ``else`` branch runs and is not one."""
    nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _dotted(node.test, aliases) == "typing.TYPE_CHECKING":
            for statement in node.body:
                for inner in ast.walk(statement):
                    if isinstance(inner, ast.Import | ast.ImportFrom):
                        nodes.add(id(inner))
    return nodes


def _annotations(tree: ast.AST) -> set[int]:
    """``id``s of every node inside an annotation: the only place a name imported for type
    checking may appear (anywhere else it would run, and could only work if rebound)."""
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
            roots.append(node.returns)
        elif isinstance(node, ast.arg | ast.AnnAssign) and node.annotation:
            roots.append(node.annotation)
    return {id(inner) for root in roots for inner in ast.walk(root)}


def _aliases(tree: ast.AST, module: str, skip: set[int] | None = None) -> dict[str, str]:
    """Local name -> the dotted name it stands for, from every import in the file but ``skip``."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if skip and id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    names[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    names[top] = top
        elif isinstance(node, ast.ImportFrom):
            source = _resolve_from(node, module)
            for alias in node.names:
                names[alias.asname or alias.name] = f"{source}.{alias.name}"
    return names


def _dotted(node: ast.expr, aliases: dict[str, str]) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    head = aliases.get(node.id)
    if head is None:
        return None
    return ".".join([head, *reversed(parts)])


def _canonical(name: str) -> str:
    """One spelling per name: a network module reached through an ``ownvitals`` module that
    imported it (``ownvitals.mail.smtplib.SMTP`` is ``smtplib.SMTP``), and the egress package's
    re-exports of its submodules."""
    parts = name.split(".")
    if parts[0] == "ownvitals":
        for i in range(1, len(parts)):
            suffix = ".".join(parts[i:])
            if _under(suffix, NETWORK_MODULES):
                return suffix
    prefix = EGRESS + "."
    if name.startswith(prefix):
        head, _, rest = name[len(prefix) :].partition(".")
        if head in EGRESS_SUBMODULES and rest:
            return prefix + rest
    return name


def _imports(tree: ast.AST, module: str, skip: set[int]) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            source = _resolve_from(node, module)
            found.append((node.lineno, source))
            found += [(node.lineno, f"{source}.{a.name}") for a in node.names]
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("import_module", "__import__") and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.append((node.lineno, first.value))
    return found


def _problems(name: str, module: str, verb: str) -> list[str]:
    """What is wrong with ``module`` importing or using ``name`` (canonical)."""
    found: list[str] = []
    if _under(name, NETWORK_MODULES) and not _under(name, ALLOWED_IMPORTS.get(module, {})):
        found.append(f"{verb} {name}: network modules are used only in {EGRESS}")
    elif name in _CLIENT_CLASSES and name not in EXEMPT.get(module, {}):
        found.append(f"{verb} {name}: outbound clients live in {EGRESS}")
    if name.rsplit(".", 1)[-1] in CONNECTION_FUNCTIONS and (
        verb == "imports" or _under(name, ("asyncio", "anyio"))
    ):
        found.append(f"{verb} {name}: raw connections live in {EGRESS}")
    if _private_sealed(name, module):
        found.append(f"{verb} {name}: private to its module")
    if name == INSTALL_WORKER and module not in WORKER_ROLE_MODULES:
        found.append(f"{verb} {name}: only the worker's entry point makes a process the worker")
    return found


def scan_source(source: str, module: str) -> list[tuple[int, str]]:
    """``(line, why)`` for everything ``module`` does outside the outbound allowlist."""
    if in_egress(module):
        return []
    tree = ast.parse(source)
    every = _aliases(tree, module)
    skip = _type_only(tree, every)
    # In an annotation a name means what the run-time imports bind; anywhere else a name bound
    # only for type checking still counts (it could only run if rebound).
    typed = _annotations(tree)
    runtime = _aliases(tree, module, skip)
    aliases = every | runtime
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            source_module = _resolve_from(node, module)
            found.append((node.lineno, f"imports {source_module}.*: names must be visible"))
    for line, raw in _imports(tree, module, skip):
        found += [(line, why) for why in _problems(_canonical(raw), module, "imports")]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute | ast.Name):
            name = _dotted(node, runtime if id(node) in typed else aliases)
            if name is not None:
                found += [(node.lineno, w) for w in _problems(_canonical(name), module, "uses")]
        elif isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = _dotted(base, aliases)
                if name is not None and _canonical(name) in _EGRESS_CONSTRUCTORS:
                    found.append((node.lineno, f"subclasses {name}: lists live in {EGRESS}"))
        elif isinstance(node, ast.Call):
            found += _scan_call(node, aliases)
    return sorted(set(found))


def _scan_call(node: ast.Call, aliases: dict[str, str]) -> list[tuple[int, str]]:
    func = node.func
    called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if called in CONNECTION_FUNCTIONS:
        return [(node.lineno, f"calls {called}: raw connections live in {EGRESS}")]
    name = _dotted(func, aliases)
    if name is None:
        return []
    name = _canonical(name)
    widened = node.args or any(k.arg in (None, "allowlist") for k in node.keywords)
    if name in _NO_ALLOWLIST_ARGUMENT and widened:
        return [(node.lineno, f"hands {name} an allowlist: lists live in {EGRESS}")]
    if name in _EGRESS_CONSTRUCTORS:
        return [(node.lineno, f"calls {name}: lists live in {EGRESS}")]
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


def scan_tree(src: Path) -> list[tuple[str, int, str]]:
    """Every violation in the ``ownvitals`` package under ``src``."""
    return [
        (module, line, why)
        for module, source in sources(src)
        for line, why in scan_source(source, module)
    ]


def exempt_uses(source: str, module: str) -> tuple[set[str], set[str]]:
    """The exempt clients ``module`` still builds (a call or a subclass) and the allowed modules
    it still imports, so a stale exemption is noticed."""
    tree = ast.parse(source)
    skip = _type_only(tree, _aliases(tree, module))
    aliases = _aliases(tree, module, skip)
    used: set[str] = set()
    for node in ast.walk(tree):
        exprs: list[ast.expr] = []
        if isinstance(node, ast.Call):
            exprs = [node.func]
        elif isinstance(node, ast.ClassDef):
            exprs = list(node.bases)
        for expr in exprs:
            name = _dotted(expr, aliases)
            if name is not None:
                used.add(_canonical(name))
    imported = {raw for _, raw in _imports(tree, module, skip)}
    allowed = {m for m in ALLOWED_IMPORTS.get(module, {}) if _under_any(imported, m)}
    return used & set(EXEMPT.get(module, {})), allowed


def _under_any(names: set[str], prefix: str) -> bool:
    return any(_under(n, (prefix,)) for n in names)


def loaded_watched_modules(entry_modules: tuple[str, ...]) -> list[str]:
    """Import ``entry_modules`` in a fresh interpreter; the web-only and client-library modules
    (``_REACHABILITY_WATCHED``) that end up in ``sys.modules``.

    This sees import-time loads only. A deferred import inside a function is not seen: only the
    runtime refusal covers it (``test_egress_runtime.py``). Any new web-only outbound call site
    must therefore call ``ownvitals.egress.refuse_in_worker`` itself, and get a runtime test."""
    import json
    import subprocess
    import sys

    probe = (
        "import importlib, json, sys\n"
        f"for name in {list(entry_modules)!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run(  # noqa: S603 - our own interpreter, a fixed probe
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, check=True
    )
    loaded = set(json.loads(result.stdout.splitlines()[-1]))
    return sorted(m for m in loaded if _under(m, _REACHABILITY_WATCHED))


def imported_modules(source: str, module: str) -> set[str]:
    """Every dotted name ``module`` imports at run time (and each ``from`` source)."""
    tree = ast.parse(source)
    skip = _type_only(tree, _aliases(tree, module))
    return {name for _, name in _imports(tree, module, skip)}
