"""Guard of PLAN §2 and roadmap Amendment 1: outbound clients are built only in
``ownvitals.egress`` (the package and its submodules; nothing outside it).

``scan_source(source, module)`` returns what ``module`` (a dotted name under ``ownvitals``) may
not do outside the egress package:

- call anything that builds an HTTP client, an opener or a raw connection, or sends a request on
  its own: ``urllib.request.urlopen``, ``build_opener``, ``install_opener``, ``OpenerDirector``,
  the ``urllib`` handlers, ``httpx.Client``, ``AsyncClient``, the transports and the module-level
  ``httpx.get`` and friends, ``http.client`` connections, ``socket.socket`` and
  ``create_connection``, ``smtplib``, ``ftplib``, Authlib's httpx clients and the Anthropic and
  OpenAI SDK clients (those build their own httpx client); subclass a client, transport,
  handler or connection;
- import a client library that bypasses the package (``requests``, ``aiohttp``, ``urllib3``,
  ``httpcore``, ``http.client``, ``pycurl``, ``websockets``, ``ftplib``, ``telnetlib`` ...), also
  through ``importlib.import_module`` or ``__import__`` with a constant name;
- build an allowlist, a transport or an opener of the package itself (``Allowlist``,
  ``AllowlistTransport``, ``build_allowlist_opener``, the deadline handlers), hand a client
  builder an allowlist, or touch any private name of the package (``_network_transport``,
  ``_worker_list`` ...): every list is chosen inside the package, never widened by a caller.

Two clients from libraries are allowed when they are handed the package's own: the Anthropic
SDK's ``AsyncAnthropic(http_client=model_async_client(...))`` (or a variable, checked in review),
and Authlib's ``AsyncOAuth2Client(transport=google_signin_transport(...), trust_env=False)`` with
no ``proxy``, ``mounts`` or ``**`` argument (those would route around the transport).

Names are resolved through imports and aliases (``import httpx as h``, ``from urllib import
request``, ``from httpx import AsyncClient as C``, ``from ownvitals.egress.core import X``). Like
the other guards this catches accidental use and makes the rest visible in review; it cannot stop
obfuscated code.

``EXEMPT`` lists the web-only call sites allowed a connection of their own, each with its reason:
only account mail, whose SMTP host is a setting (roadmap Amendment 1). A worker module never
imports one (``WORKER_PACKAGES``, ``WORKER_ENTRYPOINTS``, checked by the tests), and each calls
``refuse_in_worker`` itself (``test_egress_runtime.py``).
"""

import ast
from pathlib import Path

EGRESS = "ownvitals.egress"
_EGRESS_DIR = Path(__file__).resolve().parent.parent / "src" / "ownvitals" / "egress"
# The package's submodules: ``ownvitals.egress.core.Allowlist`` is ``ownvitals.egress.Allowlist``.
EGRESS_SUBMODULES = frozenset(p.stem for p in _EGRESS_DIR.glob("*.py") if p.stem != "__init__")
# Packages that run in the worker: they may never import an exempt module.
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
# fresh interpreter and fails if an exempt module, or a client library they use, is loaded.
WORKER_ENTRYPOINTS = (
    "ownvitals.egress",
    "ownvitals.config",
    "ownvitals.log",
    "ownvitals.db",
    "ownvitals.jobs",
)

_CALLS = frozenset(
    {
        "urllib.request.urlopen",
        "urllib.request.build_opener",
        "urllib.request.install_opener",
        "urllib.request.OpenerDirector",
        "urllib.request.HTTPHandler",
        "urllib.request.HTTPSHandler",
        "urllib.request.ProxyHandler",
        "urllib.request.FTPHandler",
        "urllib.request.FileHandler",
        "urllib.request.DataHandler",
        "urllib.request.URLopener",
        "urllib.request.FancyURLopener",
        "urllib.request.urlretrieve",
        "httpx.Client",
        "httpx.AsyncClient",
        "httpx.HTTPTransport",
        "httpx.AsyncHTTPTransport",
        "httpx.request",
        "httpx.stream",
        "httpx.get",
        "httpx.post",
        "httpx.put",
        "httpx.patch",
        "httpx.delete",
        "httpx.head",
        "httpx.options",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
        "socket.socket",
        "socket.create_connection",
        "socket.socketpair",
        "socket.fromfd",
        "smtplib.SMTP",
        "smtplib.SMTP_SSL",
        "smtplib.LMTP",
        "ftplib.FTP",
        "ftplib.FTP_TLS",
        "asyncio.open_connection",
        "asyncio.open_unix_connection",
        "authlib.integrations.httpx_client.AsyncOAuth2Client",
        "authlib.integrations.httpx_client.OAuth2Client",
        "authlib.integrations.httpx_client.AsyncOAuth1Client",
        "authlib.integrations.httpx_client.OAuth1Client",
        "anthropic.Anthropic",
        "anthropic.AsyncAnthropic",
        "anthropic.AnthropicBedrock",
        "anthropic.AsyncAnthropicBedrock",
        "anthropic.AnthropicVertex",
        "anthropic.AsyncAnthropicVertex",
        "anthropic.DefaultHttpxClient",
        "anthropic.DefaultAsyncHttpxClient",
        "openai.OpenAI",
        "openai.AsyncOpenAI",
        "ssl.get_server_certificate",
        "xmlrpc.client.ServerProxy",
        # The lists and what applies them are built inside the package only.
        f"{EGRESS}.Allowlist",
        f"{EGRESS}.AllowlistTransport",
        f"{EGRESS}.build_allowlist_opener",
        f"{EGRESS}.DeadlineHTTPSHandler",
        f"{EGRESS}.DeadlineHTTPSConnection",
    }
)
# The package's client builders: allowed only without an allowlist (each list is its own).
_NO_ALLOWLIST_ARGUMENT = frozenset(
    {
        f"{EGRESS}.withings_async_client",
        f"{EGRESS}.model_async_client",
        f"{EGRESS}.google_signin_transport",
        f"{EGRESS}.google_async_client",
        f"{EGRESS}.withings_code_exchange_client",
    }
)

# Any call by these attribute names, whatever the object (an event loop, a socket): raw
# connections around the package.
_CALL_ATTRIBUTES = frozenset(
    {
        "create_connection",
        "create_unix_connection",
        "create_datagram_endpoint",
        "sock_connect",
        "open_connection",
        "open_unix_connection",
    }
)
# Subclassing these builds a client or transport outside the allowlist.
_BASES = frozenset(
    {
        "httpx.Client",
        "httpx.AsyncClient",
        "httpx.HTTPTransport",
        "httpx.AsyncHTTPTransport",
        "httpx.BaseTransport",
        "httpx.AsyncBaseTransport",
        "urllib.request.BaseHandler",
        "urllib.request.OpenerDirector",
        "urllib.request.HTTPSHandler",
        "urllib.request.HTTPHandler",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
        "socket.socket",
        "smtplib.SMTP",
        "smtplib.SMTP_SSL",
        "smtplib.LMTP",
        "authlib.integrations.httpx_client.AsyncOAuth2Client",
        f"{EGRESS}.Allowlist",
        f"{EGRESS}.AllowlistTransport",
    }
)
MODEL_CLIENT = f"{EGRESS}.model_async_client"
GOOGLE_TRANSPORT = f"{EGRESS}.google_signin_transport"
_MODEL_SDK_CLIENT = "anthropic.AsyncAnthropic"
_OAUTH_CLIENT = "authlib.integrations.httpx_client.AsyncOAuth2Client"
# Arguments that would let Authlib's client route around its transport (``None`` is ``**``).
_ROUTING_KEYWORDS = frozenset({None, "proxy", "proxies", "mounts", "app"})
_MODULES = frozenset(
    {
        "requests",
        "aiohttp",
        "urllib3",
        "httpcore",
        "http.client",
        "pycurl",
        "websocket",
        "websockets",
        "ftplib",
        "telnetlib",
        "httpx2",
        "xmlrpc.client",
        "imaplib",
        "poplib",
        "nntplib",
        "smtpd",
    }
)

# module -> clients it may build (by a call or a subclass), with the reason.
EXEMPT: dict[str, dict[str, str]] = {
    "ownvitals.mail": {
        "smtplib.SMTP": "web only: account mail; the SMTP host is a setting (Amendment 1)",
        "smtplib.SMTP_SSL": "web only: account mail",
    },
}


def in_egress(module: str) -> bool:
    return module == EGRESS or module.startswith(EGRESS + ".")


def _blocked_module(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in _MODULES)


def _private_egress_name(name: str) -> bool:
    if not in_egress(name):
        return False
    parts = name[len(EGRESS) + 1 :].split(".") if name != EGRESS else []
    return any(p.startswith("_") and not (p.startswith("__") and p.endswith("__")) for p in parts)


def _resolve_from(node: ast.ImportFrom, module: str) -> str:
    if not node.level:
        return node.module or ""
    base = module.split(".")[: len(module.split(".")) - node.level]
    return ".".join([*base, node.module] if node.module else base)


def _aliases(tree: ast.AST, module: str) -> dict[str, str]:
    """Local name -> the dotted name it stands for, from every import in the file."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
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


_AUTHLIB = "authlib.integrations.httpx_client."


def _canonical(name: str) -> str:
    # ``from urllib import request`` gives ``urllib.request.urlopen`` already; ``import
    # urllib.request`` binds ``urllib``, so ``urllib.request.urlopen`` too. Authlib re-exports,
    # and so does ``ownvitals.egress`` for its submodules.
    name = name.replace("authlib.integrations.httpx_client.oauth2_client.", _AUTHLIB)
    prefix = EGRESS + "."
    if name.startswith(prefix):
        head, _, rest = name[len(prefix) :].partition(".")
        if head in EGRESS_SUBMODULES and rest:
            return prefix + rest
    return name


def _imports(tree: ast.AST, module: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
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


def scan_source(source: str, module: str) -> list[tuple[int, str]]:
    """``(line, why)`` for every outbound client ``module`` builds outside the allowlist."""
    if in_egress(module):
        return []
    tree = ast.parse(source)
    aliases = _aliases(tree, module)
    exempt = EXEMPT.get(module, {})
    found: list[tuple[int, str]] = []
    for line, name in _imports(tree, module):
        if _blocked_module(name):
            found.append((line, f"imports {name}: outbound clients live in {EGRESS}"))
        if _private_egress_name(name):
            found.append((line, f"imports {name}: private to {EGRESS}"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute | ast.Name):
            name = _dotted(node, aliases)
            if name is not None and _private_egress_name(name):
                found.append((node.lineno, f"uses {name}: private to {EGRESS}"))
            continue
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = _dotted(base, aliases)
                if name is None:
                    continue
                name = _canonical(name)
                if name in _BASES and name not in exempt:
                    found.append((node.lineno, f"subclasses {name}: clients live in {EGRESS}"))
            continue
        if not isinstance(node, ast.Call):
            continue
        found += _scan_call(node, aliases, exempt)
    return sorted(set(found))


def _scan_call(
    node: ast.Call, aliases: dict[str, str], exempt: dict[str, str]
) -> list[tuple[int, str]]:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _CALL_ATTRIBUTES:
        return [(node.lineno, f"calls .{func.attr}: raw connections live in {EGRESS}")]
    name = _dotted(func, aliases)
    if name is None:
        return []
    name = _canonical(name)
    if name in _NO_ALLOWLIST_ARGUMENT:
        if node.args or any(k.arg in (None, "allowlist") for k in node.keywords):
            return [(node.lineno, f"hands {name} an allowlist: lists live in {EGRESS}")]
        return []
    if name not in _CALLS or name in exempt:
        return []
    if name == _MODEL_SDK_CLIENT and _hands_the_model_client(node, aliases):
        return []
    if name == _OAUTH_CLIENT and _hands_the_google_transport(node, aliases):
        return []
    return [(node.lineno, f"calls {name}: outbound clients live in {EGRESS}")]


def _is_call_of(value: ast.expr, target: str, aliases: dict[str, str]) -> bool:
    if not isinstance(value, ast.Call):
        return False
    name = _dotted(value.func, aliases)
    return name is not None and _canonical(name) == target


def _hands_the_model_client(call: ast.Call, aliases: dict[str, str]) -> bool:
    """``http_client=`` is a variable (built by ``model_async_client``, checked in review) or a
    direct call of it; ``None``, another constant or any other expression is refused."""
    for keyword in call.keywords:
        if keyword.arg == "http_client":
            value = keyword.value
            return isinstance(value, ast.Name) or _is_call_of(value, MODEL_CLIENT, aliases)
    return False


def _hands_the_google_transport(call: ast.Call, aliases: dict[str, str]) -> bool:
    """``transport=google_signin_transport(...)`` (a direct call), ``trust_env=False``, and no
    argument that routes around the transport."""
    if any(k.arg in _ROUTING_KEYWORDS for k in call.keywords):
        return False
    given = {k.arg: k.value for k in call.keywords}
    transport, trust_env = given.get("transport"), given.get("trust_env")
    return (
        transport is not None
        and _is_call_of(transport, GOOGLE_TRANSPORT, aliases)
        and isinstance(trust_env, ast.Constant)
        and trust_env.value is False
    )


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


def exempt_calls_used(source: str, module: str) -> set[str]:
    """The exempt clients ``module`` still builds, by a call or a subclass (so a stale exemption
    is noticed)."""
    tree = ast.parse(source)
    aliases = _aliases(tree, module)
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
    return used & set(EXEMPT.get(module, {}))


def loaded_exempt_modules(entry_modules: tuple[str, ...]) -> list[str]:
    """Import ``entry_modules`` in a fresh interpreter; the exempt modules (and their client
    libraries) that end up in ``sys.modules``.

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
    # ``http.client`` and ``httpcore`` are what the allowlisted urllib and httpx paths run on.
    libraries = sorted(_MODULES - {"http.client", "httpcore"})
    watched = (*EXEMPT, "authlib", "smtplib", *libraries)
    return sorted(m for m in loaded if any(m == w or m.startswith(w + ".") for w in watched))


def imported_modules(source: str, module: str) -> set[str]:
    """Every dotted name ``module`` imports (and each ``from`` source)."""
    return {name for _, name in _imports(ast.parse(source), module)}
