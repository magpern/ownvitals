import asyncio
import asyncio.selector_events
import signal
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from ownvitals import __main__ as entry
from ownvitals.__main__ import GRACEFUL_SHUTDOWN_SECONDS, UVICORN_LOOP, Server, main
from ownvitals.egress import OutboundBlocked, worker_allowlist_installed


@pytest.fixture(autouse=True)
def _restore_urllib_opener() -> Iterator[None]:
    """``main`` installs web's process-wide urllib opener; put the pytest process's back."""
    saved = vars(urllib.request)["_opener"]
    yield
    urllib.request.install_opener(saved)  # pyright: ignore[reportArgumentType]


def _runs(monkeypatch: pytest.MonkeyPatch) -> list[Server]:
    servers: list[Server] = []
    monkeypatch.setattr(Server, "run", lambda self, sockets=None: servers.append(self))
    return servers


def _settings_of(config: uvicorn.Config) -> dict[str, Any]:
    return {
        "host": config.host,
        "port": config.port,
        "log_config": config.log_config,
        "loop": config.loop,
        "access_log": config.access_log,
        "proxy_headers": config.proxy_headers,
        "timeout_graceful_shutdown": config.timeout_graceful_shutdown,
    }


@pytest.mark.usefixtures("restore_logging")
def test_main_runs_uvicorn_with_the_json_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = _runs(monkeypatch)
    main(["--host", "0.0.0.0", "--port", "9000"])  # noqa: S104 - what the container passes
    (server,) = servers
    assert isinstance(server.config.app, FastAPI)
    assert server.app is server.config.app
    assert _settings_of(server.config) == {
        "host": "0.0.0.0",  # noqa: S104
        "port": 9000,
        "log_config": None,
        "loop": UVICORN_LOOP,
        # The access log would record webhook paths, which carry a secret; X-Forwarded-For is
        # read by ownvitals itself, from trusted proxies only.
        "access_log": False,
        "proxy_headers": False,
        # Open requests get a bounded time after SIGTERM.
        "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS,
    }


@pytest.mark.usefixtures("restore_logging")
def test_serving_installs_the_deny_all_urllib_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    """#10: a library calling ``urlopen`` in web is refused before anything is sent, for every
    scheme, and web does not become the worker (its own clients keep working)."""
    _runs(monkeypatch)
    main([])
    for url in (
        "https://oauth2.googleapis.com/token",
        "https://wbsapi.withings.net/v2/oauth2",
        "http://127.0.0.1:9/",
        "file:///etc/passwd",
    ):
        with pytest.raises((OutboundBlocked, urllib.error.URLError)) as refused:
            urllib.request.urlopen(url, timeout=1)  # noqa: S310 - the refusal is the point
        if isinstance(refused.value, urllib.error.URLError):
            assert "unknown url type" in str(refused.value.reason)
    assert not worker_allowlist_installed()


def test_a_subcommand_does_not_install_the_web_opener(monkeypatch: pytest.MonkeyPatch) -> None:
    before = vars(urllib.request)["_opener"]
    monkeypatch.setattr(entry, "run_command", lambda args, settings: 0)
    assert main(["jobs", "stats"]) == 0
    assert vars(urllib.request)["_opener"] is before


def test_uvicorn_runs_the_standard_selector_loop() -> None:
    """ADR 0009: the egress hook sees only the standard library's selector loop; uvicorn's
    "auto" picks uvloop when it is installed, and its "asyncio" is the proactor loop on Windows."""
    expected = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "asyncio"
    assert expected == UVICORN_LOOP
    config = entry.server_config(FastAPI(), "127.0.0.1", 8000)
    factory = config.get_loop_factory()
    assert factory is not None
    loop = factory()
    try:
        assert type(loop).__module__.startswith("asyncio.")
        assert isinstance(loop, asyncio.selector_events.BaseSelectorEventLoop)
    finally:
        loop.close()


@pytest.mark.usefixtures("restore_logging")
def test_main_defaults_to_localhost_8000(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = _runs(monkeypatch)
    main([])
    (server,) = servers
    assert (server.config.host, server.config.port) == ("127.0.0.1", 8000)


@pytest.mark.usefixtures("restore_logging")
def test_main_logs_as_json_at_the_configured_level(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import logging

    _runs(monkeypatch)
    monkeypatch.setenv("OWNVITALS_LOG_LEVEL", "warning")
    main([])
    assert logging.getLogger().level == logging.WARNING
    logging.getLogger("ownvitals.test").warning("hello")
    assert json.loads(capsys.readouterr().out)["message"] == "hello"


def test_the_exit_signal_tells_the_event_streams_to_end() -> None:
    class Hub:
        closed = 0

        def close_threadsafe(self) -> None:
            self.closed += 1

    app = FastAPI()
    server = Server(entry.server_config(app, "127.0.0.1", 8000), app)
    server.handle_exit(signal.SIGTERM, None)  # before the lifespan: no hub yet, still exits
    assert server.should_exit
    app.state.event_hub = Hub()
    server.handle_exit(signal.SIGTERM, None)
    assert app.state.event_hub.closed == 1
