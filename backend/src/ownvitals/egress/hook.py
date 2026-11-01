"""Runtime egress enforcement: a PEP 578 audit hook in every process (ADR 0009).

``install_egress_hook(kind, settings)`` (``kind``: ``web``, ``worker`` or ``cli``) is called
first thing in each entry point. It adds one audit hook, which cannot be removed; a second call
with the same kind does nothing (the first policy stays), with another kind it raises. For an
event that does not start with ``socket.`` the hook does one set lookup (process creation,
below) and returns; otherwise it decides:

- **Resolution** (the audit events ``socket.getaddrinfo`` and ``socket.gethostbyname``, which
  ``gethostbyname_ex`` raises too): only a host in the process's resolvable set: its kind's
  egress hosts (web: Google sign-in and the Withings code exchange; any kind: the worker list
  once ``install_worker_allowlist`` installed it), the configured database hosts, and the SMTP
  host in web and the CLI. No context is required: asyncio resolves in an executor thread,
  where context variables do not reach.
- **Reverse lookups** (``socket.gethostbyaddr``, ``socket.getnameinfo``): always refused.
- **Socket creation** (``socket.__new__``): only ``AF_INET``, ``AF_INET6`` and ``AF_UNIX``; a raw
  or packet socket is refused before it exists (its sends raise no audit event).
- **Connections** (``socket.connect``, which ``connect_ex`` raises too, ``sendto``,
  ``sendmsg``): ``AF_INET``/``AF_INET6`` only inside a connection scope, which only the
  package's transports (around a send, after their allowlist check) and ``ownvitals.mail``
  (around one SMTP session, ``smtp_session``) open; ``AF_UNIX`` only to a configured database
  socket; every other family is refused. ``bind``, ``listen`` and ``accept`` are not checked.
  One exception, Windows only: the standard library's own ``socket.socketpair`` emulation
  connects to its own loopback listener (the selector loop's self-pipe), recognised by its code
  object.
- **Process creation** that raises an audit event (``subprocess.Popen``, ``os.system``,
  ``os.exec*``, ``os.posix_spawn*``, ``os.spawn*``, ``os.fork``, ``os.forkpty``,
  ``os.startfile``, ``_winapi.CreateProcess``): always refused. No ownvitals process starts
  another, and a child would run without this hook. ``_posixsubprocess.fork_exec`` raises no
  event (``multiprocessing``'s spawn and forkserver methods use it): the static guard refuses
  it and ``multiprocessing`` in ``src/`` (ADR 0009 item 7).

The connection scope is a context variable, so work started inside it inherits it: a task
created or a ``to_thread`` call made inside the scope keeps it for as long as it runs (a
``threading.Thread`` or a ``run_in_executor`` call does not). Package code never starts one
inside a scope (the guard checks it).

A refusal raises ``EgressRefused`` (a ``PermissionError``) and logs one line with the event, the
host (or the address family) and the process kind: never a URL, a path, a query or a payload.

The hook sees what goes through Python's ``socket`` module. The probe of ADR 0009 shows that the
standard library's selector loop does (lookups in an executor thread, connections in the task)
and that uvloop and the proactor loop do not: ``install_egress_hook`` refuses to run while
``uvloop`` is importable, and ``require_standard_event_loop()`` refuses any other running loop.
A future change of event loop re-runs that probe first.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import socket
import sys
import threading
from asyncio.selector_events import BaseSelectorEventLoop
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

from sqlalchemy.engine import make_url

from ownvitals.egress.core import (
    GOOGLE_SIGNIN_HOSTS,
    WITHINGS_API_HOST,
    _printable_host,  # the package's own; the guard refuses private names outside it
    installed_worker_hosts,
)

if TYPE_CHECKING:
    from ownvitals.config import Settings

logger = logging.getLogger("ownvitals.egress")

Kind = Literal["web", "worker", "cli"]
KINDS: Final = frozenset(get_args(Kind))
WEB_HOSTS: Final = GOOGLE_SIGNIN_HOSTS | {WITHINGS_API_HOST}
POSTGRES_PORT: Final = 5432
_RESOLVE: Final = frozenset(
    {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"}
)
_REVERSE: Final = frozenset({"socket.gethostbyaddr", "socket.getnameinfo"})
_CONNECT: Final = frozenset(
    {"socket.connect", "socket.connect_ex", "socket.sendto", "socket.sendmsg"}
)
_INET: Final = frozenset({socket.AF_INET, socket.AF_INET6})
_AF_UNIX: Final = getattr(socket, "AF_UNIX", None)
_FAMILIES: Final = _INET | ({_AF_UNIX} if _AF_UNIX is not None else frozenset())
_PROCESS: Final = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        "_winapi.CreateProcess",  # Windows, under subprocess
    }
)
# A Python function only where the platform has no socketpair (Windows); None elsewhere.
_SOCKETPAIR_CODE: Final = getattr(socket.socketpair, "__code__", None)
_STANDARD_LOOP_MODULES: Final = frozenset(
    {"asyncio.unix_events", "asyncio.windows_events", "asyncio.selector_events"}
)


class EgressRefused(PermissionError):
    """A network operation the egress hook refused. ``event`` is the audit event; ``target`` is
    the host or the address family, never a path, a query or a payload."""

    def __init__(self, event: str, target: str) -> None:
        super().__init__(f"egress refused: {event} ({target})")
        self.event = event
        self.target = target


@dataclass(frozen=True)
class _Policy:
    kind: Kind
    hosts: frozenset[str]  # resolvable host names, lower case, no trailing dot
    unix_paths: frozenset[str]  # AF_UNIX connect targets (the database's socket)

    def resolvable(self, host: str) -> bool:
        return host in self.hosts or host in installed_worker_hosts()


_scope: ContextVar[str | None] = ContextVar("ownvitals_egress_scope", default=None)
_policy: _Policy | None = None
_install_lock = threading.Lock()
_logging = threading.local()


@contextmanager
def _connection_scope(owner: str) -> Iterator[None]:
    """Connections in this context (this thread or task) may reach the network: opened only by
    the package's transports around a send, after their allowlist check."""
    token = _scope.set(owner)
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def smtp_session() -> Iterator[None]:
    """The connection scope of one SMTP session; only ``ownvitals.mail`` opens it (the guard
    refuses it anywhere else)."""
    with _connection_scope("smtp"):
        yield


def _normalise(host: object) -> str | None:
    if isinstance(host, bytes | bytearray):
        host = bytes(host).decode("ascii", "replace")
    if not isinstance(host, str):
        return None
    return host.strip().rstrip(".").lower()


def _database_endpoints(settings: Settings) -> tuple[set[str], set[str]]:
    """The host names and Unix socket paths of every configured database URL (only the host,
    the port and the query's ``host`` are read)."""
    hosts: set[str] = set()
    paths: set[str] = set()
    urls = [settings.database_url, settings.migrations_database_url, settings.maint_database_url]
    for secret in urls:
        if secret is None:
            continue
        url = make_url(secret.get_secret_value())
        query_host = url.query.get("host")
        names = [url.host] if url.host else []
        names += [query_host] if isinstance(query_host, str) else list(query_host or ())
        for name in (n for item in names for n in item.split(",") if n):
            if name.startswith("/"):
                paths.add(f"{name.rstrip('/')}/.s.PGSQL.{url.port or POSTGRES_PORT}")
            elif (host := _normalise(name)) is not None:
                hosts.add(host)
    return hosts, paths


def _policy_for(kind: Kind, settings: Settings) -> _Policy:
    hosts, paths = _database_endpoints(settings)
    if kind == "web":
        hosts |= WEB_HOSTS
    if kind in ("web", "cli") and settings.smtp_host:
        hosts.add(_normalise(settings.smtp_host) or "")
    return _Policy(kind, frozenset(hosts - {""}), frozenset(paths))


def install_egress_hook(kind: Kind, settings: Settings) -> None:
    """Install the process's egress hook (module docstring). Idempotent for the same kind; any
    other kind raises, and so does an importable ``uvloop``, which the hook cannot see."""
    global _policy
    if kind not in KINDS:
        raise ValueError(f"unknown process kind {kind!r}")
    with _install_lock:
        if _policy is not None:
            if _policy.kind != kind:
                raise RuntimeError(f"the egress hook is already installed for {_policy.kind}")
            return
        if importlib.util.find_spec("uvloop") is not None:
            raise RuntimeError("uvloop is importable: its sockets bypass the egress hook")
        policy = _policy_for(kind, settings)
        sys.addaudithook(_audit)
        _policy = policy


def installed_kind() -> Kind | None:
    return None if _policy is None else _policy.kind


def require_standard_event_loop() -> None:
    """Refuse to run on an event loop the egress hook cannot see: only the standard library's
    selector loop fires the socket audit events (ADR 0009's probe; uvloop and the proactor loop
    connect in C)."""
    loop = asyncio.get_running_loop()
    cls = type(loop)
    if cls.__module__ not in _STANDARD_LOOP_MODULES or not isinstance(loop, BaseSelectorEventLoop):
        raise RuntimeError(f"refusing to run on {cls.__module__}.{cls.__qualname__}")


def _family(sock: Any) -> int | None:
    family = getattr(sock, "family", None)
    return int(family) if family is not None else None


def _family_name(family: int | None) -> str:
    try:
        return socket.AddressFamily(family).name if family is not None else "unknown"
    except ValueError:
        return str(family)


def _check_connect(policy: _Policy, event: str, args: tuple[Any, ...]) -> None:
    sock, address = (*args, None, None)[:2]
    family = _family(sock)
    if address is None and event in ("socket.sendto", "socket.sendmsg"):
        return  # a send on a connected socket: its connect was checked
    if family in _INET:
        if _scope.get() is not None:
            return
        # Frames: this function, ``_audit``, then the caller of ``connect``.
        if (
            event == "socket.connect"
            and _SOCKETPAIR_CODE is not None
            and sys._getframe(2).f_code is _SOCKETPAIR_CODE
        ):
            return
    elif family is not None and family == _AF_UNIX:
        path = address.decode("utf-8", "replace") if isinstance(address, bytes) else address
        if isinstance(path, str) and path in policy.unix_paths:
            return
    _refuse(policy, event, _family_name(family))


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not event.startswith("socket."):
        if event in _PROCESS and (policy := _policy) is not None:
            _refuse(policy, event, "process creation", "process creation refused")
        return
    policy = _policy
    if policy is None:  # pragma: no cover - installed together with the policy
        return
    if event == "socket.__new__":
        family = args[1] if len(args) > 1 else None
        if family not in _FAMILIES:
            _refuse(policy, event, _family_name(family if isinstance(family, int) else None))
    elif event in _RESOLVE:
        host = _normalise(args[0]) if args else None
        if host is None or policy.resolvable(host):  # None: a local address, no lookup
            return
        _refuse(policy, event, _printable_host(host))
    elif event in _REVERSE:
        _refuse(policy, event, "reverse lookup")
    elif event in _CONNECT:
        _check_connect(policy, event, args)


def _refuse(
    policy: _Policy, event: str, target: str, message: str = "outbound connection refused"
) -> None:
    if not getattr(_logging, "busy", False):  # a log handler's own socket is checked, not logged
        _logging.busy = True
        try:
            logger.warning(
                message,
                extra={"event": event, "target": target, "process_kind": policy.kind},
            )
        finally:
            _logging.busy = False
    raise EgressRefused(event, target)


def resolvable_hosts() -> frozenset[str]:
    """The installed policy's resolvable host names (empty before the hook is installed)."""
    return frozenset() if _policy is None else _policy.hosts | installed_worker_hosts()
