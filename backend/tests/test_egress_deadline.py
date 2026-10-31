"""The total deadline of a request: an answer that trickles in a byte at a time never outlasts it,
although every byte arrives well inside the socket (urllib) or per-read (httpx) timeout; and no
urllib request starts after the call's deadline. Against a TLS server on loopback with a
throwaway certificate; never the network."""

import asyncio
import contextlib
import datetime
import ipaddress
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from ownvitals import egress
from ownvitals.egress import (
    DeadlineHTTPSConnection,
    DeadlineHTTPSHandler,
    RequestDeadline,
    call_deadline,
    call_deadline_until,
    core,
    install_worker_allowlist,
    opener,
    tls_client_context,
    withings_async_client,
)

DRIP_SECONDS = 0.05
API = "https://wbsapi.withings.net"


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


def _certificate(directory: Path) -> tuple[str, str]:
    now = datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ownvitals test loopback")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


@dataclass
class DripServer:
    """``drip``: answer 200 with a JSON body sent one byte every ``DRIP_SECONDS``; ``fast``: at
    once. Records each request's path and whether the client went away before the end."""

    port: int = 0
    ca: str = ""
    mode: str = "drip"
    paths: list[str] = field(default_factory=lambda: list[str]())
    cut: list[bool] = field(default_factory=lambda: list[bool]())
    accepts: int = 0  # connections opened by the client


def _serve(server: DripServer, listener: socket.socket, context: ssl.SSLContext) -> None:
    while True:
        try:
            raw, _ = listener.accept()
        except OSError:
            return
        server.accepts += 1
        threading.Thread(target=_one, args=(server, raw, context), daemon=True).start()


def _one(server: DripServer, raw: socket.socket, context: ssl.SSLContext) -> None:
    try:
        conn = context.wrap_socket(raw, server_side=True)
    except (OSError, ssl.SSLError):
        return
    with conn:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        server.paths.append(lines[0].split(" ")[1])
        length = next(
            (
                int(v)
                for k, _, v in (h.partition(": ") for h in lines[1:])
                if k.lower() == "content-length"
            ),
            0,
        )
        while len(rest) < length:  # read the request body: closing on unread data resets
            rest += conn.recv(4096)
        body = b'{"status": 0, "body": {}}' + b" " * 200  # synthetic
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        )
        try:
            if server.mode == "drip":
                for i in range(len(body)):
                    conn.sendall(body[i : i + 1])
                    time.sleep(DRIP_SECONDS)
            else:
                conn.sendall(body)
            server.cut.append(False)
        except OSError:
            server.cut.append(True)


@pytest.fixture
def drip(tmp_path: Path) -> Iterator[DripServer]:
    cert, key = _certificate(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    listener = socket.create_server(("127.0.0.1", 0))
    server = DripServer(port=listener.getsockname()[1], ca=cert)
    threading.Thread(target=_serve, args=(server, listener, server_context), daemon=True).start()
    try:
        yield server
    finally:
        listener.close()


@pytest.fixture
def urlopen(drip: DripServer, monkeypatch: pytest.MonkeyPatch) -> Callable[..., bytes]:
    """``urlopen`` of a path through the worker's deadline handler against the loopback server
    (the allowlist itself is tested elsewhere: it would refuse this host and port)."""
    deadline_opener = urllib.request.OpenerDirector()
    for handler in (
        DeadlineHTTPSHandler(context=tls_client_context(cafile=drip.ca)),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        deadline_opener.add_handler(handler)
    monkeypatch.setattr(urllib.request, "_opener", deadline_opener)

    def get(path: str, **kwargs: Any) -> bytes:
        url = f"https://127.0.0.1:{drip.port}{path}"
        with urllib.request.urlopen(url, **kwargs) as response:  # noqa: S310
            return response.read()

    return get


def _until(check: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.02)


def test_a_slow_drip_answer_is_cut_at_the_request_deadline(
    drip: DripServer, urlopen: Callable[..., bytes]
) -> None:
    started = time.monotonic()
    with pytest.raises(RequestDeadline):
        urlopen("/v2/user", timeout=1)  # each byte comes in 50 ms
    elapsed = time.monotonic() - started
    assert 0.9 <= elapsed < 1.6
    _until(lambda: drip.cut == [True])  # the server saw the connection go


def test_the_call_deadline_cuts_a_request_with_a_longer_timeout(
    drip: DripServer, urlopen: Callable[..., bytes]
) -> None:
    started = time.monotonic()
    with call_deadline(0.8), pytest.raises(RequestDeadline):
        urlopen("/v2/user", timeout=30)
    assert time.monotonic() - started < 1.4


def test_mutation_without_the_deadline_a_drip_outlasts_the_socket_timeout(
    drip: DripServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same drip through urllib's plain handler: a 1 s socket timeout never fires, so the
    request lasts as long as the server likes (here the whole body, over 10 s if not stopped)."""
    plain = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.HTTPSHandler(context=tls_client_context(cafile=drip.ca)),
        urllib.request.HTTPErrorProcessor(),
    ):
        plain.add_handler(handler)
    finished: list[float] = []

    def call() -> None:
        started = time.monotonic()
        with contextlib.suppress(Exception):  # only the time matters here
            plain.open(f"https://127.0.0.1:{drip.port}/v2/user", timeout=1).read()
        finished.append(time.monotonic() - started)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(2.0)
    assert thread.is_alive()
    assert not finished


def test_no_request_starts_after_the_call_deadline(
    drip: DripServer, urlopen: Callable[..., bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requests each fast, but the whole runs past the bound: the request due after the deadline
    is refused before it is sent. Time is a fake clock that moves 10 "seconds" per request (the
    bound is 25), so exactly three fit whatever the machine's speed. The watchdog is not armed
    (its real timer would mask a missing check): this proves the refusal before connecting."""
    now = [1000.0]
    monkeypatch.setattr(opener, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(opener._Watchdog, "arm", lambda self, sock: None)  # pyright: ignore[reportPrivateUsage]
    drip.mode = "fast"
    with call_deadline(25), pytest.raises(RequestDeadline):  # noqa: PT012 - the loop is the call
        for _ in range(10):
            urlopen("/v2/measure")
            now[0] += 10.0  # the caller working after each answer
    assert len(drip.paths) == 3  # starts at 0, 10 and 20 are inside the bound; 30 is not
    time.sleep(0.3)  # real time: give a stray request the chance to show up
    assert (len(drip.paths), drip.accepts) == (3, 3)  # not even a connection for the fourth


def test_a_request_due_after_the_deadline_never_connects(
    drip: DripServer, urlopen: Callable[..., bytes]
) -> None:
    drip.mode = "fast"
    with call_deadline(0.2):
        time.sleep(0.3)  # the caller working between two requests
        with pytest.raises(RequestDeadline):
            urlopen("/v2/user")
    time.sleep(0.2)
    assert (drip.accepts, drip.paths) == (0, [])


def test_a_request_inside_its_deadline_is_untouched(
    drip: DripServer, urlopen: Callable[..., bytes]
) -> None:
    drip.mode = "fast"
    with call_deadline(5):
        for _ in range(3):
            assert urlopen("/v2/user").startswith(b'{"status": 0')
    _until(lambda: len(drip.cut) == 3)
    assert drip.cut == [False] * 3
    assert threading.active_count() < 20  # every watchdog ended with its response


def test_the_worker_opener_uses_the_deadline_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    installed = install_worker_allowlist()
    handlers: list[Any] = installed.handlers  # pyright: ignore[reportAttributeAccessIssue]
    https = [h for h in handlers if hasattr(h, "https_open")]
    assert len(https) == 1
    assert isinstance(https[0], DeadlineHTTPSHandler)
    seen: list[Any] = []
    monkeypatch.setattr(
        DeadlineHTTPSHandler, "do_open", lambda self, cls, req, **kw: seen.append(cls)
    )
    https[0].https_open(urllib.request.Request(f"{API}/v2/user"))  # noqa: S310 - not opened
    assert seen == [DeadlineHTTPSConnection]


def test_the_request_deadline_rules() -> None:
    now = time.monotonic()
    assert egress.request_deadline(30) == pytest.approx(now + 30, abs=0.5)
    for unset in (None, object(), True, 0, -1, float("nan"), float("inf")):
        assert egress.request_deadline(unset) == pytest.approx(
            now + egress.REQUEST_FALLBACK_SECONDS, abs=0.5
        )
    with call_deadline(5):
        assert egress.request_deadline(30) == pytest.approx(now + 5, abs=0.5)
        with call_deadline(60):  # a nested block keeps the earlier deadline
            assert egress.request_deadline(30) == pytest.approx(now + 5, abs=0.5)
        assert egress.request_deadline(2) == pytest.approx(now + 2, abs=0.5)
    assert egress.request_deadline(30) == pytest.approx(now + 30, abs=0.5)
    with pytest.raises(ValueError, match="positive"), call_deadline(0):
        pass


def test_a_thread_that_starts_late_uses_up_the_bound(
    drip: DripServer, urlopen: Callable[..., bytes]
) -> None:
    drip.mode = "fast"
    at = time.monotonic() + 0.3
    outcome: list[BaseException | None] = []

    def late() -> None:
        time.sleep(0.5)  # queued behind other calls
        try:
            with call_deadline_until(at):
                urlopen("/v2/user")
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    thread = threading.Thread(target=late, daemon=True)
    thread.start()
    thread.join(5)
    assert len(outcome) == 1
    assert isinstance(outcome[0], RequestDeadline)
    time.sleep(0.2)
    assert (drip.accepts, drip.paths) == (0, [])


def test_a_late_thread_keeps_only_what_is_left_of_the_bound() -> None:
    at = time.monotonic() + 1.0
    time.sleep(0.4)
    with call_deadline_until(at):
        left = egress.request_deadline(30) - time.monotonic()
    with call_deadline(1.0):
        whole = egress.request_deadline(30) - time.monotonic()
    assert left == pytest.approx(0.6, abs=0.15)
    assert whole == pytest.approx(1.0, abs=0.15)
    with pytest.raises(TypeError, match="instant"), call_deadline_until(True):  # pyright: ignore[reportArgumentType]
        pass


@pytest.mark.parametrize("at", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_deadline_fails_closed(at: float) -> None:
    with pytest.raises(ValueError, match="finite"), call_deadline_until(at):
        pass


# --- httpx: the clients' total deadline --------------------------------------------------------


class ToLoopback(httpx.AsyncBaseTransport):
    """The network transport of the test: whatever passed the checks goes to the drip server."""

    def __init__(self, drip: DripServer) -> None:
        self._port = drip.port
        self._network = httpx.AsyncHTTPTransport(
            trust_env=False, verify=tls_client_context(cafile=drip.ca)
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(host="127.0.0.1", port=self._port)
        return await self._network.handle_async_request(request)

    async def aclose(self) -> None:
        await self._network.aclose()


@pytest.fixture
def worker() -> None:
    install_worker_allowlist()


async def test_a_trickling_body_is_cut_at_the_clients_total_deadline(
    worker: None, drip: DripServer
) -> None:
    """Each byte arrives well within httpx's per-read timeout; only the total ends it."""
    started = time.monotonic()
    async with withings_async_client(total_seconds=0.5, inner=ToLoopback(drip)) as client:
        with pytest.raises(RequestDeadline):
            await client.post(f"{API}/v2/user", data={"action": "getdevice"})
    assert 0.4 <= time.monotonic() - started < 1.5
    assert drip.paths == ["/v2/user"]
    await asyncio.to_thread(_until, lambda: drip.cut == [True])


async def test_a_streamed_body_is_cut_too(worker: None, drip: DripServer) -> None:
    async with (
        withings_async_client(total_seconds=0.5, inner=ToLoopback(drip)) as client,
        client.stream("POST", f"{API}/v2/user", data={"action": "getdevice"}) as response,
    ):
        with pytest.raises(RequestDeadline):
            async for _ in response.aiter_bytes():
                pass


async def test_a_prompt_answer_is_untouched(worker: None, drip: DripServer) -> None:
    drip.mode = "fast"
    async with withings_async_client(total_seconds=2, inner=ToLoopback(drip)) as client:
        response = await client.post(f"{API}/v2/user", data={"action": "getdevice"})
    assert response.json() == {"status": 0, "body": {}}


class Slow(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"status": 0})


class Stalled(httpx.AsyncByteStream):
    """One byte at once, then nothing for a long time: no next chunk to check the clock at."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"{"
        await asyncio.sleep(5)
        yield b"}"


async def test_a_stalled_body_ends_at_the_total_deadline(worker: None) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Stalled())

    started = time.monotonic()
    async with withings_async_client(
        total_seconds=0.3, inner=httpx.MockTransport(answer)
    ) as client:
        with pytest.raises(RequestDeadline):
            await client.get(f"{API}/v2/user")
    assert time.monotonic() - started < 2


async def test_a_slow_answer_ends_at_the_total_deadline(worker: None) -> None:
    started = time.monotonic()
    async with withings_async_client(total_seconds=0.2, inner=Slow()) as client:
        with pytest.raises(RequestDeadline):
            await client.get(f"{API}/v2/user")
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("seconds", [0, -1.0, float("nan"), float("inf"), True, "10"])
def test_a_total_deadline_is_positive_and_finite(worker: None, seconds: Any) -> None:
    for builder in (
        withings_async_client,
        egress.google_async_client,
        egress.withings_code_exchange_client,
    ):
        with pytest.raises(ValueError, match="positive, finite"):
            builder(total_seconds=seconds)
