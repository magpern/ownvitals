"""The worker's ``/metrics`` listener (PLAN section 4.4 "Observability", #167).

The one module outside ``ownvitals.egress`` that may open a server socket: the egress guard
admits ``asyncio.start_server`` and the stream types here and nowhere else (ADR 0009, note of
2026-11-09). It **listens**, it never connects, and it is never routed through SWAG: Prometheus
on the home server scrapes it on an internal address.

- **Bind**: an IP literal in its canonical spelling, loopback or private only (127.0.0.0/8,
  10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, ``::1``, ``fc00::/7``). A host name, a wildcard
  (``0.0.0.0``, ``::``), ``0.0.0.0/8``, broadcast, link-local, IPv4-mapped or 6to4 IPv6, a zone id
  and every public address raise ``BindRefused`` before a socket exists. A literal needs no
  lookup, so the egress hook sees no resolution.
- **Requests**: ``GET /metrics`` only. A request line or header line over 4096 bytes is a 400,
  more than 64 header lines a 400, another method a 405, another path a 404.
- **Time**: a client that sends nothing is dropped after ``read_timeout``; the whole exchange,
  ``drain()`` included, runs under ``deadline``.
- **Connections**: at most ``max_connections`` at a time; one more is closed at once.
- A client that resets or goes away is not an error.

The body is the registry's text exposition: counts, ages and states aggregated across users,
never an id or a value (``ownvitals.metrics``).
"""

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from typing import Final

from ownvitals.metrics import REGISTRY, Registry

logger = logging.getLogger(__name__)

CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
LINE_LIMIT: Final = 4096
MAX_HEADER_LINES: Final = 64
_CLOSE_SECONDS: Final = 1.0
_ALLOWED_NETWORKS: Final = tuple(
    ipaddress.ip_network(n)
    for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fc00::/7")
)


class BindRefused(ValueError):
    """The metrics listener refused a bind address that is not loopback or private."""


def check_bind(host: str) -> str:
    """The canonical literal when ``host`` may be bound, else ``BindRefused``."""
    if not isinstance(host, str) or "%" in host:
        raise BindRefused("the metrics bind must be an IP literal without a zone")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused("the metrics bind must be an IP literal") from None
    if str(address) != host:
        raise BindRefused("the metrics bind must be spelled canonically")
    if not any(address in network for network in _ALLOWED_NETWORKS):
        raise BindRefused("the metrics bind must be a loopback or private address")
    return host


def _answer(status: str, body: bytes, content_type: str = "text/plain; charset=utf-8") -> bytes:
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
    )
    return head.encode("ascii") + body


async def _request(reader: asyncio.StreamReader, read_timeout: float) -> bytes:
    """The answer for one request: its line, then the headers up to the blank line, read so
    that closing the socket leaves nothing unread."""
    try:
        async with asyncio.timeout(read_timeout):
            line = await reader.readline()
    except ValueError:  # over LINE_LIMIT (the stream's limit)
        return _answer("400 Bad Request", b"bad request\n")
    parts = line.decode("latin-1").split()
    for _ in range(MAX_HEADER_LINES):
        try:
            header = await reader.readline()
        except ValueError:
            return _answer("400 Bad Request", b"bad request\n")
        if header in (b"\r\n", b"\n", b""):
            break
    else:
        return _answer("400 Bad Request", b"bad request\n")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return _answer("400 Bad Request", b"bad request\n")
    if parts[0] != "GET":
        return _answer("405 Method Not Allowed", b"method not allowed\n")
    if parts[1] != "/metrics":
        return _answer("404 Not Found", b"not found\n")
    return b""


@dataclass
class _Limits:
    read_timeout: float
    deadline: float
    max_connections: int
    active: int = 0


async def _close(writer: asyncio.StreamWriter, *, abort: bool = False) -> None:
    """Close the connection; a client that stops reading cannot hold it open: what is still
    buffered after ``_CLOSE_SECONDS`` (or at once, with ``abort``) is dropped."""
    if abort:
        writer.transport.abort()
    else:
        writer.close()
    try:
        async with asyncio.timeout(_CLOSE_SECONDS):
            await writer.wait_closed()
    except TimeoutError:
        writer.transport.abort()
    except OSError:
        pass


async def _handle(
    registry: Registry,
    limits: _Limits,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    if limits.active >= limits.max_connections:
        await _close(writer)
        return
    limits.active += 1
    failed = False
    try:
        async with asyncio.timeout(limits.deadline):
            answer = await _request(reader, limits.read_timeout)
            if not answer:
                answer = _answer("200 OK", registry.render().encode("utf-8"), CONTENT_TYPE)
            writer.write(answer)
            await writer.drain()
    except OSError:  # TimeoutError, ConnectionResetError, BrokenPipeError
        failed = True  # a silent, slow or vanished client: drop it
    finally:
        limits.active -= 1
        await _close(writer, abort=failed)


async def serve_metrics(
    host: str,
    port: int,
    registry: Registry = REGISTRY,
    *,
    read_timeout: float = 5.0,
    deadline: float = 10.0,
    max_connections: int = 4,
) -> asyncio.Server:
    """Start the listener on an internal address; the caller closes the returned server."""
    check_bind(host)
    if not 0 <= port <= 65535:
        raise BindRefused("the metrics port must be 0 to 65535")
    limits = _Limits(read_timeout, deadline, max_connections)
    server = await asyncio.start_server(
        lambda r, w: _handle(registry, limits, r, w), host, port, limit=LINE_LIMIT
    )
    logger.info("metrics listener started", extra={"port": server_port(server)})
    return server


def server_port(server: asyncio.Server) -> int:
    """The bound port (useful with port 0)."""
    return int(server.sockets[0].getsockname()[1])
