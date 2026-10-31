"""The whole suite refuses non-loopback sockets before a packet leaves (``netguard``).

These tests never make a real connection, even when the guard is broken: each builds its own guard
on top of recording stand-ins for the socket functions, and checks what reached them."""

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from netguard import NetworkBlocked, install

PUBLIC = [("192.0.2.1", 443), ("198.51.100.7", 80), ("203.0.113.9", 443), ("2001:db8::1", 443)]


class Recorder:
    """Stands where the real socket functions stand; remembers what got through the guard."""

    def __init__(self) -> None:
        self.connects: list[Any] = []
        self.lookups: list[Any] = []

    def connect(self, _sock: socket.socket, address: Any) -> None:
        self.connects.append(address)

    def connect_ex(self, _sock: socket.socket, address: Any) -> int:
        self.connects.append(address)
        return 0

    def getaddrinfo(self, host: Any, *_: Any, **__: Any) -> list[Any]:
        self.lookups.append(host)
        return []


@pytest.fixture
def guarded() -> Iterator[Recorder]:
    seen = Recorder()
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(socket.socket, "connect", seen.connect)
        patched.setattr(socket.socket, "connect_ex", seen.connect_ex)
        patched.setattr(socket, "getaddrinfo", seen.getaddrinfo)
        install(patched)  # the guard under test, wrapping the recorder
        yield seen


@pytest.mark.parametrize("address", PUBLIC)
def test_connecting_to_a_public_address_is_refused_before_it_reaches_the_socket(
    guarded: Recorder, address: tuple[str, int]
) -> None:
    sock = socket.socket(socket.AF_INET6 if ":" in address[0] else socket.AF_INET)
    with sock:
        with pytest.raises(NetworkBlocked, match="may not connect"):
            sock.connect(address)
        with pytest.raises(NetworkBlocked):
            sock.connect_ex(address)
    assert guarded.connects == []


@pytest.mark.parametrize("name", ["api.anthropic.com", "models.invalid", "192.0.2.1"])
def test_resolving_a_public_name_is_refused_before_any_lookup(guarded: Recorder, name: str) -> None:
    with pytest.raises(NetworkBlocked, match="may not resolve"):
        socket.getaddrinfo(name, 443)
    assert guarded.lookups == []


@pytest.mark.parametrize("address", [("127.0.0.1", 5432), ("::1", 80), ("localhost", 8000)])
def test_loopback_passes_through_to_the_socket(guarded: Recorder, address: tuple[str, int]) -> None:
    with socket.socket() as sock:
        sock.connect(address)
    socket.getaddrinfo("localhost", 80)
    assert guarded.connects == [address]
    assert guarded.lookups == ["localhost"]


def _guarded() -> bool:
    return all(
        f.__module__ == "netguard"
        for f in (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo)
    )


def test_the_suite_guard_is_installed_and_lets_loopback_work() -> None:
    assert _guarded()  # identity, not an attempt: nothing here can reach a network
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname(), timeout=2):
            server.accept()[0].close()


def test_a_test_calling_monkeypatch_undo_keeps_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "patched")
    monkeypatch.undo()
    assert socket.gethostname() != "patched"
    assert _guarded()


@pytest.mark.allow_network
def test_the_marker_switches_the_guard_off() -> None:
    # Proves only that the opt-out is wired; "models.invalid" never resolves (RFC 6761), and
    # the lookup is made by the test's own stand-in so nothing is sent.
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(socket, "getaddrinfo", lambda *_a, **_k: [])
        assert socket.getaddrinfo("models.invalid", 443) == []
