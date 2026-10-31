"""No backend test may send a packet to a non-loopback address (AGENTS.md, security rules).

``install(monkeypatch)`` replaces ``socket.socket.connect``, ``connect_ex`` and
``socket.getaddrinfo`` with versions that raise ``NetworkBlocked`` before anything is sent unless
the target is loopback (``127.0.0.0/8``, ``::1``, ``localhost``) or a Unix socket. The throwaway
Postgres, in-process fakes, asyncio's own self-pipe and local servers all stay on loopback. A test
that needs more says so with ``@pytest.mark.allow_network`` (nothing does today).
"""

import ipaddress
import socket
from typing import Any

import pytest


class NetworkBlocked(AssertionError):
    """A test tried to reach a non-loopback host."""


def _loopback(host: Any) -> bool:
    if host is None or host == "" or host == "localhost":
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _check_address(address: Any) -> None:
    if isinstance(address, tuple) and address and not _loopback(address[0]):
        raise NetworkBlocked(f"tests may not connect to {address[0]!r}: use a fake or loopback")


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        _check_address(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check_address(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _loopback(host):
            raise NetworkBlocked(f"tests may not resolve {host!r}: use a fake or loopback")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
