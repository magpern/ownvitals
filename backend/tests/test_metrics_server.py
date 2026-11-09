"""The worker's ``/metrics`` listener (#167): bind refusal, request handling, read timeout, line
limit, the deadline around ``drain()``, the connection cap and a client that resets. Loopback
only; the registries are synthetic (one counter), no real data."""

import asyncio
import contextlib
import logging
import socket
import struct
import time

import pytest

from ownvitals.metrics import Counter, Registry
from ownvitals.metrics_server import (
    CONTENT_TYPE,
    BindRefused,
    check_bind,
    serve_metrics,
    server_port,
)

REFUSED = (
    "localhost",
    "metrics.example.test",
    "",
    " 127.0.0.1",
    "127.000.000.001",
    "0:0:0:0:0:0:0:1",  # ::1, spelled otherwise
    "0.0.0.0",  # noqa: S104 - a wildcard, refused
    "::",
    "::0.0.0.0",
    "0.0.0.1",  # 0.0.0.0/8
    "255.255.255.255",
    "224.0.0.1",
    "169.254.1.1",
    "fe80::1",
    "fe80::1%eth0",
    "127.0.0.1%1",
    "::1%1",
    "::ffff:0.0.0.0",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:8.8.8.8",
    "2002:a00::1",  # 6to4 of 10.0.0.0
    "2002:7f00:1::1",  # 6to4 of 127.0.0.1
    "100.64.0.1",
    "192.0.0.1",
    "198.18.0.1",
    "2001:db8::1",
    "8.8.8.8",
    "2606:4700::1",
)
ALLOWED = (
    "127.0.0.1",
    "127.0.0.2",
    "10.1.2.3",
    "172.16.0.1",
    "172.31.255.254",
    "192.168.1.10",
    "::1",
    "fd00::1",
)


@pytest.mark.parametrize("host", REFUSED)
def test_a_bind_outside_loopback_and_private_literals_is_refused(host: str) -> None:
    with pytest.raises(BindRefused):
        check_bind(host)


@pytest.mark.parametrize("host", ALLOWED)
def test_loopback_and_private_literals_are_allowed(host: str) -> None:
    assert check_bind(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "::ffff:127.0.0.1", "localhost"])  # noqa: S104
async def test_the_listener_refuses_before_any_socket(host: str) -> None:
    with pytest.raises(BindRefused):
        await serve_metrics(host, 0, Registry())


def _registry(body_bytes: int = 0) -> Registry:
    registry = Registry()
    counter = registry.register(Counter("synthetic_scrapes_total", "A synthetic counter."))
    counter.inc()
    if body_bytes:
        big = "x" * body_bytes
        registry.render = lambda: big  # type: ignore[method-assign]
    return registry


async def _exchange(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), timeout=5)
    except ConnectionResetError:  # closed by the server with the request unread
        answer = b""
    writer.close()
    with contextlib.suppress(ConnectionResetError):
        await writer.wait_closed()
    return answer


async def test_get_metrics_answers_the_exposition_and_nothing_else() -> None:
    registry = _registry()
    server = await serve_metrics("127.0.0.1", 0, registry)
    port = server_port(server)
    try:
        ok = await _exchange(port, b"GET /metrics HTTP/1.1\r\nHost: w\r\nAccept: */*\r\n\r\n")
        head, _, body = ok.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert f"Content-Type: {CONTENT_TYPE}".encode() in head
        assert b"Cache-Control: no-store" in head
        assert body == registry.render().encode()
        cases = {
            b"GET /other HTTP/1.1\r\n\r\n": b"404",
            b"GET /metrics/x HTTP/1.1\r\n\r\n": b"404",
            b"POST /metrics HTTP/1.1\r\n\r\n": b"405",
            b"HEAD /metrics HTTP/1.1\r\n\r\n": b"405",
            b"nonsense\r\n\r\n": b"400",
            b"GET /metrics\r\n\r\n": b"400",
        }
        for request, status in cases.items():
            answer = await _exchange(port, request)
            assert answer.startswith(b"HTTP/1.1 " + status), request
            assert b"synthetic_scrapes_total" not in answer, request
    finally:
        server.close()
        await server.wait_closed()


LIMIT = 4096  # the issue's request-line limit, not read from the module under test


async def test_a_request_line_over_the_limit_is_a_400() -> None:
    server = await serve_metrics("127.0.0.1", 0, _registry())
    try:
        line = b"GET /metrics" + b"a" * LIMIT + b" HTTP/1.1\r\n\r\n"
        answer = await _exchange(server_port(server), line)
        assert answer.startswith(b"HTTP/1.1 400")
        header = b"GET /metrics HTTP/1.1\r\nX-Pad: " + b"a" * LIMIT + b"\r\n\r\n"
        assert (await _exchange(server_port(server), header)).startswith(b"HTTP/1.1 400")
        fits = b"GET /metrics HTTP/1.1\r\nX-Pad: " + b"a" * (LIMIT - 100) + b"\r\n\r\n"
        assert (await _exchange(server_port(server), fits)).startswith(b"HTTP/1.1 200")
        many = b"GET /metrics HTTP/1.1\r\n" + b"X-A: 1\r\n" * 65 + b"\r\n"
        assert (await _exchange(server_port(server), many)).startswith(b"HTTP/1.1 400")
    finally:
        server.close()
        await server.wait_closed()


async def test_a_client_that_sends_nothing_is_dropped_after_the_read_timeout() -> None:
    server = await serve_metrics("127.0.0.1", 0, _registry(), read_timeout=0.2, deadline=30)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server_port(server))
        started = time.monotonic()
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        assert time.monotonic() - started < 2
        writer.close()
    finally:
        server.close()
        await server.wait_closed()


def _stuck_client(port: int) -> socket.socket:
    """Asks for the metrics and never reads: the server's ``drain()`` blocks."""
    stuck = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stuck.connect(("127.0.0.1", port))
    stuck.sendall(b"GET /metrics HTTP/1.1\r\n\r\n")
    return stuck


async def test_the_deadline_covers_drain_and_frees_the_slot() -> None:
    """One slot. A client that never reads a large answer holds it until the deadline; after
    that the next client is served. Without a deadline around ``drain()`` it would hold it."""
    server = await serve_metrics(
        "127.0.0.1", 0, _registry(32 * 1024 * 1024), deadline=0.5, max_connections=1
    )
    port = server_port(server)
    stuck = _stuck_client(port)
    try:
        await asyncio.sleep(0.2)
        assert await _exchange(port, b"GET /metrics HTTP/1.1\r\n\r\n") == b""  # slot taken
        await asyncio.sleep(0.8)
        answer = await _exchange(port, b"GET /metrics HTTP/1.1\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 200 OK")
    finally:
        stuck.close()
        server.close()
        await server.wait_closed()


async def test_connections_over_the_cap_are_closed_at_once() -> None:
    server = await serve_metrics(
        "127.0.0.1", 0, _registry(), read_timeout=5, deadline=10, max_connections=2
    )
    port = server_port(server)
    held = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
    try:
        await asyncio.sleep(0.1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        started = time.monotonic()
        assert await asyncio.wait_for(reader.read(), timeout=3) == b""
        assert time.monotonic() - started < 1
        writer.close()
        for held_reader, held_writer in held:
            held_writer.write(b"GET /metrics HTTP/1.1\r\n\r\n")
            answer = await asyncio.wait_for(held_reader.read(), timeout=5)
            assert answer.startswith(b"HTTP/1.1 200 OK")
    finally:
        for _, held_writer in held:
            held_writer.close()
        server.close()
        await server.wait_closed()


async def test_a_client_that_resets_is_not_an_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    server = await serve_metrics("127.0.0.1", 0, _registry(8 * 1024 * 1024), deadline=5)
    port = server_port(server)
    try:
        reset = _stuck_client(port)
        await asyncio.sleep(0.2)
        # SO_LINGER with a zero timeout: close sends RST, so the server's write fails.
        reset.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        reset.close()
        await asyncio.sleep(0.3)
        answer = await _exchange(port, b"GET /metrics HTTP/1.1\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 200 OK")
    finally:
        server.close()
        await server.wait_closed()
        loop.set_exception_handler(previous)
    assert unhandled == []
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
