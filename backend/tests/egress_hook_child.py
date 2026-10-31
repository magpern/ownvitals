"""Run by ``test_egress_hook.py`` in a fresh interpreter, because the egress hook (ADR 0009) cannot
be removed and so never goes into the pytest process:
``python egress_hook_child.py <kind>``. It installs the hook for ``kind``, runs every case and
prints one JSON object: per case the exception (class, and the reason's class for a wrapped
one), the refusals the hook logged meanwhile, and the connections the loopback listener
accepted.

**No network, even with the hook mutated.** Before anything else the resolver functions are
replaced by fakes that raise the same audit events as CPython's and answer ``127.0.0.1``: the
loopback listener, or the SMTP fake for ``mailpit``. Every host name here is synthetic
(``example.invalid``, ``db.example.test``) or one of the egress lists' own.
"""

import _socket
import asyncio
import importlib
import io
import json
import logging
import os
import smtplib
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

LINUX = sys.platform.startswith("linux")
AF_UNIX: int = getattr(socket, "AF_UNIX", -1)  # Linux-only cases use it
OUTSIDE = "example.invalid"
DATABASE = "db.example.test"
SMTP = "mailpit"
EGRESS_HOST = {"web": "www.googleapis.com", "worker": "wbsapi.withings.net", "cli": DATABASE}


class Listener:
    """Accepts and closes every connection on 127.0.0.1, counting them."""

    def __init__(self, family: int = socket.AF_INET, path: str | None = None) -> None:
        self.sock = socket.socket(family)
        self.sock.bind(path if path is not None else ("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1] if path is None else 0
        self.accepted = 0
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            connection, _ = self.sock.accept()
            self.accepted += 1
            connection.close()


LISTENER = Listener()
ROUTES: dict[str, int] = {}  # host -> port on 127.0.0.1; any other host: the listener


def _host(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def fake_getaddrinfo(
    host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
) -> list[Any]:
    sys.audit("socket.getaddrinfo", host, port, family, type, proto)
    kind = type or socket.SOCK_STREAM
    target = ROUTES.get(_host(host), LISTENER.port)
    return [(socket.AF_INET, kind, proto, "", ("127.0.0.1", target))]


def fake_gethostbyname(host: str) -> str:
    sys.audit("socket.gethostbyname", host)
    return "127.0.0.1"


def fake_gethostbyname_ex(host: str) -> tuple[str, list[str], list[str]]:
    sys.audit("socket.gethostbyname", host)  # CPython raises the same event for both
    return host, [], ["127.0.0.1"]


def fake_gethostbyaddr(address: str) -> tuple[str, list[str], list[str]]:
    sys.audit("socket.gethostbyaddr", address)
    return "localhost", [], [address]


def fake_getnameinfo(sockaddr: tuple[str, int], flags: int) -> tuple[str, str]:
    sys.audit("socket.getnameinfo", sockaddr)
    return "localhost", str(sockaddr[1])


for module in (socket, _socket):
    module.getaddrinfo = fake_getaddrinfo  # type: ignore[assignment]
    module.gethostbyname = fake_gethostbyname  # type: ignore[assignment]
    module.gethostbyname_ex = fake_gethostbyname_ex  # type: ignore[assignment]
    module.gethostbyaddr = fake_gethostbyaddr  # type: ignore[assignment]
    module.getnameinfo = fake_getnameinfo  # type: ignore[assignment]

from pydantic import SecretStr  # noqa: E402

from ownvitals.config import Settings  # noqa: E402
from ownvitals.egress import (  # noqa: E402
    google_async_client,
    install_egress_hook,
    install_worker_allowlist,
    require_standard_event_loop,
    smtp_session,
    withings_async_client,
)
from ownvitals.log import configure_logging  # noqa: E402
from ownvitals.mail import Mailer, MailMessage  # noqa: E402
from smtpfake import SmtpFake  # noqa: E402

KIND = sys.argv[1]
OTHER_KIND = "cli" if KIND != "cli" else "web"
SCRATCH = Path(tempfile.mkdtemp(prefix="ov-hook-"))
DB_SOCKET_DIR = SCRATCH / "pg"
DB_SOCKET_DIR.mkdir()
LOG = io.StringIO()
REFUSALS: list[list[str]] = []


class Refusals(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "outbound connection refused":
            fields = ("event", "target", "process_kind")
            REFUSALS.append([str(getattr(record, name, "")) for name in fields])


def settings(**values: Any) -> Settings:
    return Settings(
        database_url=SecretStr(f"postgresql+psycopg://ownvitals_app@{DATABASE}:5432/ownvitals"),
        maint_database_url=SecretStr(
            f"postgresql+psycopg://ownvitals_maint@/ownvitals?host={DB_SOCKET_DIR}"
        ),
        smtp_host=SMTP,
        smtp_tls="none",
        public_base_url="https://vitals.example.org",
        **values,
    )


def selector(coroutine: Any) -> Any:
    return asyncio.run(coroutine, loop_factory=asyncio.SelectorEventLoop)


# --- the cases ----------------------------------------------------------------------------------


def unix_connect(path: Path) -> None:
    client = socket.socket(AF_UNIX)
    try:
        client.connect(str(path))
    finally:
        client.close()


def connect_outside_scope(*, ex: bool = False) -> None:
    address = socket.getaddrinfo(EGRESS_HOST[KIND], 443)[0][4]  # allowed to resolve
    client = socket.socket()
    client.settimeout(2)
    try:
        if ex:
            client.connect_ex(address)
        else:
            client.connect(address)
    finally:
        client.close()


async def resolve_in_executor(host: str) -> None:
    await asyncio.get_running_loop().getaddrinfo(host, 5432)  # an executor thread, no context


async def fetch(client: Any, url: str) -> None:
    async with client:
        await client.get(url)


async def deliver() -> dict[str, Any]:
    async with SmtpFake() as fake:
        ROUTES[SMTP] = fake.port
        mailer = Mailer(settings(smtp_port=fake.port))
        await mailer.send(MailMessage("someone@example.com", "Hello", "A synthetic message.\n"))
    return {"messages": len(fake.messages), "ehlo": fake.ehlo_names}


def smtp_by_hand() -> None:
    with smtp_session():
        smtplib.SMTP(SMTP, 25, local_hostname="localhost", timeout=2)


def minidom_parse_uri() -> None:
    from xml.dom import minidom

    builder = minidom.getDOMImplementation().createDOMBuilder(1, None)  # type: ignore[attr-defined]
    builder.parseURI(f"http://{OUTSIDE}/x.xml")


def numpy_loadtxt() -> None:
    import numpy as np

    np.loadtxt(f"http://{OUTSIDE}/x.csv")


def standard_loop() -> None:
    async def check() -> None:
        require_standard_event_loop()

    selector(check())


CASES: dict[str, Callable[[], Any]] = {
    "resolve_outside": lambda: socket.getaddrinfo(OUTSIDE, 443),
    "gethostbyname_outside": lambda: socket.gethostbyname(OUTSIDE),
    "gethostbyname_ex_outside": lambda: socket.gethostbyname_ex(OUTSIDE),
    "gethostbyaddr": lambda: socket.gethostbyaddr("127.0.0.1"),
    "getnameinfo": lambda: socket.getnameinfo(("127.0.0.1", 80), 0),
    "fresh_opener": lambda: urllib.request.build_opener().open(
        f"http://{OUTSIDE}/secret-path/report?token=abc123", timeout=2
    ),
    "create_connection": lambda: socket.create_connection((OUTSIDE, 80), timeout=2),
    "numpy_loadtxt": numpy_loadtxt,
    "minidom_parse_uri": minidom_parse_uri,
    "database_resolves": lambda: socket.getaddrinfo(DATABASE, 5432),
    "database_resolves_in_executor": lambda: selector(resolve_in_executor(DATABASE)),
    "connect_outside_scope": connect_outside_scope,
    "connect_ex_outside_scope": lambda: connect_outside_scope(ex=True),
    "udp_sendto": lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
        b"x", ("127.0.0.1", LISTENER.port)
    ),
    "standard_loop": standard_loop,
    "second_install_same_kind": lambda: install_egress_hook(KIND, settings()),  # type: ignore[arg-type]
    "second_install_other_kind": lambda: install_egress_hook(OTHER_KIND, settings()),  # type: ignore[arg-type]
}
if LINUX:
    OTHER_SOCKET = SCRATCH / "other.sock"
    Listener(AF_UNIX, str(OTHER_SOCKET))
    Listener(AF_UNIX, str(DB_SOCKET_DIR / ".s.PGSQL.5432"))
    CASES["unix_other"] = lambda: unix_connect(OTHER_SOCKET)
    CASES["unix_database"] = lambda: unix_connect(DB_SOCKET_DIR / ".s.PGSQL.5432")
if KIND == "web":
    CASES["egress_client"] = lambda: selector(
        fetch(google_async_client(), "https://www.googleapis.com/oauth2/v3/certs")
    )
if KIND == "worker":
    CASES["egress_client"] = lambda: selector(
        fetch(withings_async_client(), "https://wbsapi.withings.net/measure")
    )
    CASES["urllib_opener"] = lambda: urllib.request.urlopen(
        "https://wbsapi.withings.net/measure", timeout=2
    )
    CASES["model_host_without_ai"] = lambda: socket.getaddrinfo("api.anthropic.com", 443)
    CASES["smtp"] = smtp_by_hand
if KIND in ("web", "cli"):
    CASES["smtp"] = lambda: selector(deliver())
if KIND == "cli":
    CASES["worker_host_before_probe"] = lambda: socket.getaddrinfo("wbsapi.withings.net", 443)
    CASES["worker_host_after_probe"] = lambda: (
        install_worker_allowlist(),
        socket.getaddrinfo("wbsapi.withings.net", 443),
    )


def describe(exc: BaseException) -> str:
    name = type(exc).__name__
    reason = getattr(exc, "reason", None)
    return f"{name}({type(reason).__name__})" if isinstance(reason, BaseException) else name


def chain(exc: BaseException | None) -> list[str]:
    names: list[str] = []
    while exc is not None and len(names) < 10:
        names.append(type(exc).__name__)
        exc = exc.__cause__ or exc.__context__
    return names


def run(name: str, case: Callable[[], Any]) -> dict[str, Any]:
    mark, before = len(REFUSALS), LISTENER.accepted
    error, extra, causes = None, None, []
    try:
        result = case()
        extra = result if isinstance(result, dict) else None
    except Exception as exc:  # recorded, never raised: the parent judges
        error, causes = describe(exc), chain(exc)
    time.sleep(0.05)  # let the listener thread count an accepted connection
    return {
        "error": error,
        "refused": REFUSALS[mark:],
        "accepted": LISTENER.accepted - before,
        "extra": extra,
        "chain": causes,
    }


def uvloop_refused() -> dict[str, Any]:
    """With a ``uvloop`` on the path the hook refuses to install, and installs once it is gone."""
    shadow = SCRATCH / "shadow"
    (shadow / "uvloop").mkdir(parents=True)
    (shadow / "uvloop" / "__init__.py").write_text("")
    sys.path.insert(0, str(shadow))
    importlib.invalidate_caches()
    try:
        install_egress_hook(KIND, settings())  # type: ignore[arg-type]
    except RuntimeError as exc:
        error: str | None = str(exc)
    else:
        error = None
    finally:
        sys.path.remove(str(shadow))
        importlib.invalidate_caches()
    return {"error": error, "refused": [], "accepted": 0, "extra": None, "chain": []}


def main() -> None:
    configure_logging("INFO", stream=LOG)
    logging.getLogger("ownvitals.egress").addHandler(Refusals())
    results = {"uvloop_importable": uvloop_refused()}
    install_egress_hook(KIND, settings())  # type: ignore[arg-type]
    if KIND == "worker":
        install_worker_allowlist(ai_enabled=False)  # as the worker's entry point will (#77)
    for name, case in CASES.items():
        results[name] = run(name, case)
    print(json.dumps({"kind": KIND, "results": results, "log": LOG.getvalue()}))
    sys.stdout.flush()
    os._exit(0)  # daemon listener threads: no clean-up wait


if __name__ == "__main__":
    main()
