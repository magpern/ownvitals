"""ADR 0009's event-loop probe: which socket audit events the running event loop raises, in which
thread, and whether the caller's context variables are visible there. Run by
``test_egress_loop_probe.py`` in a fresh interpreter (an audit hook cannot be removed), or by hand
before any change of event loop: ``python tests/egress_loop_probe.py [asyncio|uvloop]``.

Loopback only: every connection goes to a listener on ``127.0.0.1``; the one name looked up is
``localhost`` (the hosts file), so no query leaves the machine. It prints one JSON object: per
scenario the events seen, each with ``main_thread`` and ``context_visible``.
"""

import asyncio
import contextlib
import json
import socket
import sys
import threading
import urllib.request
from contextvars import ContextVar
from typing import Any

EVENTS = ("socket.getaddrinfo", "socket.gethostbyname", "socket.connect")
MARK: ContextVar[str | None] = ContextVar("probe_mark", default=None)
SEEN: list[dict[str, Any]] = []


def hook(event: str, _args: tuple[Any, ...]) -> None:
    if event in EVENTS:
        SEEN.append(
            {
                "event": event,
                "main_thread": threading.current_thread() is threading.main_thread(),
                "context_visible": MARK.get() is not None,
            }
        )


def listener() -> int:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)

    def accept() -> None:
        while True:
            connection, _ = server.accept()
            connection.close()

    threading.Thread(target=accept, daemon=True).start()
    return server.getsockname()[1]


async def open_connection(port: int) -> None:
    MARK.set("set")
    # By name: asyncio skips the lookup for a numeric address.
    _, writer = await asyncio.open_connection("localhost", port, family=socket.AF_INET)
    writer.close()


async def to_thread() -> None:
    MARK.set("set")
    await asyncio.to_thread(socket.getaddrinfo, "127.0.0.1", 80)


async def run_in_executor() -> None:
    MARK.set("set")
    await asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, "127.0.0.1", 80)


def blocking_urlopen(port: int) -> None:
    MARK.set("set")
    with contextlib.suppress(OSError):  # the listener closes at once; the events are what count
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)


def main(loop_name: str) -> None:
    if loop_name == "uvloop":
        import uvloop  # pyright: ignore[reportMissingImports]

        factory = uvloop.new_event_loop  # pyright: ignore[reportUnknownMemberType]
    else:
        factory = asyncio.SelectorEventLoop
    port = listener()
    sys.addaudithook(hook)
    scenarios = {
        "open_connection": lambda: asyncio.run(open_connection(port), loop_factory=factory),
        "to_thread": lambda: asyncio.run(to_thread(), loop_factory=factory),
        "run_in_executor": lambda: asyncio.run(run_in_executor(), loop_factory=factory),
        "blocking_urlopen": lambda: blocking_urlopen(port),
    }
    results: dict[str, list[dict[str, Any]]] = {}
    for name, scenario in scenarios.items():
        SEEN.clear()
        scenario()
        results[name] = list(SEEN)
    loop = factory()
    results["loop_class"] = [{"class": f"{type(loop).__module__}.{type(loop).__qualname__}"}]
    loop.close()
    print(json.dumps(results))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "asyncio")
