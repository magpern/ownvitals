"""The reviewed classification of every network-capable module ``src/`` uses (PR #55 round 2).

``egress_closure`` computes which modules can reach the network; this table says what ``src/``
may do with each one it imports or references. A capable module that is not here is refused
("not classified"): a new dependency or a new import cannot slip in unreviewed. Changing this
file is a security-critical change.

- **denied**: refused wherever it is imported or referenced (a carve-out of a by-design prefix);
- **by_design**: the whole module is allowed, for inbound or driver code only (the web
  framework, the server, the database toolkit: their hosts are the listener or a setting);
- **allowed**: only ``names`` (and, with ``exceptions``, the module's own exception classes) may
  be referenced from it, by an import, a call, a bare reference, an attribute value, an alias, a
  ``partial`` argument or a chain; anything else in the module is refused, so a new connection
  API in it is refused by default.

A key ending in ``.*`` covers the package and its submodules; the most specific key wins.
``only_in`` limits an entry to those modules (elsewhere the module is unclassified).

``EDGE_CUTS``: modules whose imports are reviewed as not a network path, with the reason; the
closure does not propagate through them (``test_egress_closure.py`` proves each reason).

``LOOP_METHODS``: the methods of ``asyncio.AbstractEventLoop`` (read from the locked stdlib) that
any object may use. Every other method of it (``create_connection``, ``sock_connect``,
``getaddrinfo`` ...) is refused as an attribute of any object, since a loop object cannot be
told apart statically.

Stale entries, names and cuts fail the guard test (entries and cuts only on Linux, where CI runs
and the closure is authoritative).
"""

from dataclasses import dataclass
from typing import Literal

Kind = Literal["denied", "by_design", "allowed"]


@dataclass(frozen=True)
class Entry:
    kind: Kind
    reason: str
    names: frozenset[str] = frozenset()
    exceptions: bool = False
    only_in: tuple[str, ...] = ()


def _allowed(reason: str, *names: str, exceptions: bool = False, only_in: tuple[str, ...] = ()):
    return Entry("allowed", reason, frozenset(names), exceptions, only_in)


TABLE: dict[str, Entry] = {
    # (b) by design: inbound or driver code.
    "fastapi": Entry("by_design", "the web framework: inbound HTTP, served by uvicorn"),
    "fastapi.responses": Entry("by_design", "response classes of the web framework"),
    "starlette.*": Entry("by_design", "the ASGI toolkit under FastAPI: inbound HTTP"),
    "uvicorn": Entry("by_design", "the ASGI server: it listens, it does not connect out"),
    "sqlalchemy.*": Entry(
        "by_design", "the database toolkit; it connects only to the configured database (N13)"
    ),
    # (a) denied: carve-outs of a by-design prefix.
    "starlette.testclient": Entry("denied", "an httpx client; tests use their own"),
    # (c) allowed names.
    "asyncio": _allowed(
        "the event loop and threads; connections, servers and loop network methods are refused",
        "AbstractEventLoop",  # a type, for the Windows selector loop policy (db.engine)
        "CancelledError",  # an exception class (jobs, worker)
        "Event",  # an in-process flag (jobs, worker)
        "FIRST_COMPLETED",  # a constant of asyncio.wait (worker.loop)
        "Lock",  # an in-process lock (crypto.keks)
        "Semaphore",  # bounds the password-hashing pool (auth.passwords, #10); no I/O of its own
        "SelectorEventLoop",  # psycopg on Windows (db.engine); its methods: LOOP_METHODS
        "Task",  # a type (jobs, worker)
        "ensure_future",  # schedules a coroutine on the running loop; no I/O of its own
        "gather",  # awaits tasks together; no I/O of its own
        "get_running_loop",  # the running loop; its network methods stay refused (LOOP_METHODS)
        "run",
        "sleep",  # a timer
        "timeout",
        "to_thread",
        "wait",  # awaits tasks; no I/O of its own
        "wait_for",  # a timeout around an awaitable; no I/O of its own
    ),
    "email.message": _allowed(
        "building a message; email.utils' make_msgid (a DNS lookup) is not used",
        "EmailMessage",
    ),
    "importlib.metadata": _allowed(
        "the installed version; its email parsing never connects",
        "PackageNotFoundError",
        "version",
    ),
    "pydantic.fields": _allowed("field metadata (uuid makes it capable)", "FieldInfo"),
    "pydantic_settings": _allowed(
        "settings from the environment and secret files; no remote sources",
        "BaseSettings",
        "PydanticBaseSettingsSource",
        "SettingsConfigDict",
    ),
    "smtplib": _allowed(
        "account mail, web only (roadmap Amendment 1): the two clients and their errors",
        "SMTP",
        "SMTP_SSL",
        exceptions=True,
        only_in=("ownvitals.mail",),
    ),
    "socket": _allowed(
        "the worker's lease owner name: gethostname() is a local system call, no lookup and no "
        "connection; every other socket name stays refused (the egress hook checks at run time)",
        "gethostname",
        only_in=("ownvitals.worker.loop",),
    ),
    "uuid": _allowed(
        "the UUID type and uuid4() (random bytes from os.urandom); uuid1() and getnode() (which "
        "can look the host name up) are not used",
        "UUID",
        "uuid4",
    ),
    "scipy.stats": _allowed(
        "statistics on in-memory arrays; capable only through scipy's docstring tooling, which "
        "imports pydoc, whose documentation server (http.server, select) is never started",
        "rankdata",
    ),
    # The ECG core (#114): filters and peak detection on in-memory arrays.
    "scipy.signal": _allowed(
        "filters and peaks on in-memory arrays; capable only through scipy's docstring tooling, "
        "which imports pydoc, whose documentation server (http.server, select) is never started",
        "butter",
        "find_peaks",
        "get_window",
        "iirnotch",
        "sosfiltfilt",
        "sosfreqz",
        "tf2sos",
    ),
    "scipy.ndimage": _allowed(
        "1-D filters on in-memory arrays; capable only through scipy's docstring tooling, which "
        "imports pydoc, whose documentation server (http.server, select) is never started",
        "maximum_filter1d",
        "uniform_filter1d",
    ),
    # The migrations' RLS helpers (ownvitals.db.rls): SQL on the migration's own connection to
    # the configured database, like sqlalchemy (N13).
    "alembic": Entry("allowed", "imported for its op proxy only (alembic.op); none of its names"),
    "alembic.op": _allowed(
        "SQL on the migration's connection to the configured database", "execute"
    ),
    # The queue's one-shot runner (#101, ADR 0005 section 3): one libpq connection to the stack's
    # database, by host, port, database and user from its argument (password from PGPASSFILE).
    # No egress hook: docs/dev/deploy.md, "db-functions and the egress hook".
    "psycopg": _allowed(
        "one libpq connection to the database named on db-functions' command line, and the two "
        "error classes it catches (psycopg re-exports them from psycopg.errors)",
        "connect",
        "Error",
        "ProgrammingError",
        only_in=("ownvitals.db.install_functions",),
    ),
    "psycopg.conninfo": _allowed(
        "parses and rebuilds db-functions' connection string to check its keys; no connection",
        "conninfo_to_dict",
        "make_conninfo",
        only_in=("ownvitals.db.install_functions",),
    ),
    # Accounts and sign-in (#10, ADR 0003). Google's JWKS and token responses are fetched through
    # ownvitals.egress; these only parse and verify what is already in memory.
    "joserfc.jwt": _allowed(
        "verifies an ID token in memory (RS256 against keys already fetched); capable only "
        "through cryptography.x509's import of email.utils, never a connection",
        "decode",
    ),
    "joserfc.jwk": _allowed(
        "builds a key set from the JWKS document ownvitals.egress fetched; it fetches nothing",
        "KeySet",
    ),
    "segno": _allowed(
        "renders the TOTP enrolment QR code as an SVG string in memory (classbeat ADR 0003 "
        "section 7); it opens no connection",
        "make",
    ),
    "fastapi.encoders": _allowed(
        "the web framework's JSON encoder for the validation-error handler (auth.setup)",
        "jsonable_encoder",
    ),
    "fastapi.exception_handlers": _allowed(
        "the web framework's default validation-error handler, wrapped so /api/auth bodies are "
        "never echoed (auth.setup)",
        "request_validation_exception_handler",
    ),
    "fastapi.exceptions": _allowed(
        "the web framework's validation error class (auth.setup)",
        "RequestValidationError",
    ),
}

EDGE_CUTS: dict[str, str] = {
    "typing_extensions": (
        "imports _socket only to read the type of its CAPI capsule (CapsuleType); "
        "it binds no socket API"
    ),
    "platform": (
        "imports socket only inside _node(), for socket.gethostname(): a local system call, "
        "no connection and no lookup"
    ),
}

# Process creation the egress hook cannot see (ADR 0009 item 7): ``_posixsubprocess.fork_exec``
# raises no audit event, and ``multiprocessing``'s spawn and forkserver methods use it; a child
# runs without the hook. Refused in ``src/`` under any name, whether capable or not.
PROCESS_MODULES: tuple[str, ...] = (
    "_posixsubprocess",
    "_winapi",
    "multiprocessing",
    "concurrent.futures.process",
)
PROCESS_NAMES: frozenset[str] = frozenset({"concurrent.futures.ProcessPoolExecutor"})
# Native calls (ADR 0009 item 7): ``ctypes`` reaches the C library's own process and socket
# functions, which raise no audit event the hook sees. Refused in ``src/`` under any name.
NATIVE_MODULES: tuple[str, ...] = ("ctypes", "_ctypes")
# Reflection by name: ``sys.modules`` hands out any loaded module past every rule that reads
# names (``_posixsubprocess`` is always loaded: ``subprocess`` imports it). Refused in ``src/``.
REFLECTION_NAMES: tuple[str, ...] = ("sys.modules", "pkgutil.resolve_name")
# Importers (the FAIL on #123, B1): ``IMPORT_CALLS`` only as a direct call with one constant name,
# which is then judged as an import; a stored, aliased-and-stored or ``getattr`` reference would
# hide the name. ``SPEC_FINDERS`` (``find_spec``, ``spec_from_*``) are refused outright.
IMPORT_CALLS: frozenset[str] = frozenset(
    {"importlib.import_module", "importlib.__import__", "builtins.__import__"}
)
SPEC_FINDERS: tuple[str, ...] = (
    "importlib.util.find_spec",
    "importlib.util.spec_from_file_location",
    "importlib.util.spec_from_loader",
)
# Module loaders (the FAIL on #123, C1): each loads a module by a name the guard cannot judge
# (``runpy.run_module``, ``pkgutil.get_loader``, ``PathFinder.find_spec``, ``_gcd_import``).
# Refused outright, with every ``importlib`` name but ``import_module`` and ``importlib.metadata``.
LOADER_MODULES: tuple[str, ...] = (
    "runpy",
    "pkgutil",
    "zipimport",
    "importlib.machinery",
    "importlib.abc",
    "importlib._bootstrap",
    "importlib._bootstrap_external",
    "_frozen_importlib",
    "_frozen_importlib_external",
    "_imp",
)
# Evaluators (the FAIL on #123, D2): they run code or load objects from text or bytes the guard
# cannot read (``exec("import ctypes")``, ``pickle.loads(...)``, ``timeit.timeit(...)``,
# ``ImportString`` on ``"ctypes:CDLL"``). Refused outright; ``re.compile`` is not ``compile``.
EVALUATORS: tuple[str, ...] = (
    "builtins.exec",
    "builtins.eval",
    "builtins.compile",
    "pickle",
    "_pickle",
    "marshal",
    "shelve",
    "timeit",
    "code",
    "codeop",
    "pydantic.ImportString",
    "pydantic.types.ImportString",
    # The sixth FAIL on #123 (B2): ``help("x")`` imports ``x`` through ``pydoc``, and
    # ``breakpoint()`` imports what ``PYTHONBREAKPOINT`` names through ``sys.breakpointhook``.
    "builtins.help",
    "builtins.breakpoint",
    "sys.breakpointhook",
    # The seventh FAIL on #123 (B2): the same importer under its saved name, and the class behind
    # the ``help`` builtin (``_sitebuiltins._Helper()("x")`` imports ``x`` through ``pydoc``).
    "sys.__breakpointhook__",
    "_sitebuiltins",
    # The fifth FAIL on #123 (F4): they run a string as code (``cProfile.run``, ``profile.run``,
    # ``trace.Trace().run``, ``pdb.run``, ``bdb.Bdb.run``) or the code in docstrings (``doctest``).
    "cProfile",
    "profile",
    "trace",
    "doctest",
    "pdb",
    "bdb",
)
# Source readers (the FAIL on #123, E2): they hand back docstrings, comments or source text,
# which a string rule never sees. Refused outright, with the name ``__doc__``.
SOURCE_READERS: tuple[str, ...] = ("inspect", "pydoc", "linecache", "tokenize", "traceback")
# Words that make a string code (the FAIL on #123, E1a): an importer, evaluator or reflection
# name as an identifier segment of any string ``src`` writes (prose: ``PROSE_WORDS`` only).
CODE_WORDS: frozenset[str] = frozenset(
    {
        "__import__",
        "import_module",
        "exec",
        "eval",
        "compile",
        "__builtins__",
        "builtins",
        "modules",
        "getattr",
        "vars",
        "globals",
        "PYTHONBREAKPOINT",  # the variable ``sys.breakpointhook`` imports by name (B2)
        # The seventh FAIL on #123 (B2): ``vars(builtins)["help"]`` and the like.
        "help",
        "breakpoint",
        "breakpointhook",
        "__breakpointhook__",
    }
)
# Strings refused by the string rules and reviewed as harmless: an exact string, or an exact line
# of a docstring or comment (stripped), by module. ``test_egress_strings.py`` pins each to ``src``.
CODE_STRINGS: frozenset[tuple[str, str]] = frozenset(
    {
        ("ownvitals.mail", "MailMessage(...)"),  # MailMessage.__repr__: never evaluated
        ("ownvitals.mail", "Mailer(...)"),  # Mailer.__repr__: never evaluated
        ("ownvitals.auth.keys", "Keys(...)"),  # Keys.__repr__: never evaluated
        ("ownvitals.auth.tables", "gen_random_uuid()"),  # a SQL server default, sent to Postgres
        ("ownvitals.auth.tables", "now()"),  # a SQL server default, sent to Postgres
    }
)
# Docstring or comment lines refused by the prose rules and reviewed as harmless: exact stripped
# lines, by module, passing only as prose there. ``test_egress_strings.py`` pins each to ``src``.
PROSE_LINES: frozenset[tuple[str, str]] = frozenset(
    {
        (  # names ctypes as an out-of-scope route
            "ownvitals.context",
            "slot descriptors, ctypes) is out of scope for runtime checks; "
            "the review gate catches it.",
        ),
        (  # names import_module as what the port leaves out
            "ownvitals.auth",
            "re-export table (PEP 562, ``import_module`` with a computed name) is not ported, "
            "because the",
        ),
        # The seventh FAIL on #123 (B1): prose naming an evaluator as a word.
        (  # the operator command, run through docker compose
            "ownvitals.cli",
            "``docker compose exec web python -m ownvitals admin grant owner@example.com``.",
        ),
        (  # the CSP forbids eval
            "ownvitals.headers",
            "everything from our own origin only, with no exception. No inline or ``eval`` script, "
            "no inline",
        ),
    }
)
# Prose words (the seventh FAIL on #123, B1), besides the last segment of every evaluator,
# importer, spec finder and loader: the module and method names an evaluated line could reach an
# importer through. ``code``, ``profile``, ``trace`` and ``abc`` are common English and are
# modules a string cannot reach without an importer, so they are not words.
PROSE_IMPORTER_WORDS: frozenset[str] = frozenset(
    {"__builtins__", "importlib", "pydoc", "_Helper", "_find_and_load", "find_module"}
    | {"find_loader", "load_module", "exec_module", "module_from_spec", "_gcd_import"}
    | {"run_module", "run_path", "resolve_name", "get_loader", "__loader__", "__spec__"}
    | {"meta_path", "path_hooks", "path_importer_cache", "PYTHONBREAKPOINT"}
)
PROSE_COMMON_WORDS: frozenset[str] = frozenset({"code", "profile", "trace", "abc"})
# A dict-display key refused by the string rules and reviewed as data: it passes only as a key.
DICT_KEY_STRINGS: frozenset[tuple[str, str]] = frozenset(
    {
        # The ECG record's key for its analysis modules and versions (``ecg/pipeline.py``).
        ("ownvitals.ecg.pipeline", "modules"),
    }
)

LOOP_METHODS: frozenset[str] = frozenset(
    {
        "add_signal_handler",  # the worker's SIGTERM/SIGINT stop (worker.__main__); no network
        "call_at",
        "call_exception_handler",
        "call_later",
        "call_soon",
        "call_soon_threadsafe",
        "close",
        "create_future",
        "create_task",
        "default_exception_handler",
        "get_debug",
        "get_exception_handler",
        "get_task_factory",
        "is_closed",
        "is_running",
        "run_forever",
        "run_in_executor",
        "run_until_complete",
        "set_debug",
        "set_default_executor",
        "set_exception_handler",
        "set_task_factory",
        "shutdown_asyncgens",
        "shutdown_default_executor",
        "stop",
        "time",
    }
)
