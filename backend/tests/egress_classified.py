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
