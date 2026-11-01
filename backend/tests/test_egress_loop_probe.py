"""ADR 0009's probe, pinned: on the standard library's selector loop every lookup and connection
raises its socket audit event. Lookups run in an executor thread that does not see the caller's
context (so the hook checks a lookup by host alone), connections run in the task with its context
(so the hook can require the connection scope). Linux only, where the service runs and CI runs;
the probe itself stays on loopback."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROBE = Path(__file__).resolve().parent / "egress_loop_probe.py"

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the service's platform: Linux"
)


def _event(event: str, main_thread: bool, context_visible: bool) -> dict[str, Any]:
    return {"event": event, "main_thread": main_thread, "context_visible": context_visible}


@pytest.fixture(scope="module")
def probe() -> dict[str, list[dict[str, Any]]]:
    done = subprocess.run(  # noqa: S603 - this interpreter, a file of this directory
        [sys.executable, str(PROBE), "asyncio"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stdout)


def test_the_probe_runs_the_standard_selector_loop(probe: dict[str, Any]) -> None:
    assert probe["loop_class"] == [{"class": "asyncio.unix_events._UnixSelectorEventLoop"}]


def test_a_connection_looks_up_in_an_executor_and_connects_in_the_task(
    probe: dict[str, Any],
) -> None:
    assert probe["open_connection"] == [
        _event("socket.getaddrinfo", main_thread=False, context_visible=False),
        _event("socket.connect", main_thread=True, context_visible=True),
    ]


def test_to_thread_carries_the_context_and_run_in_executor_does_not(
    probe: dict[str, Any],
) -> None:
    assert probe["to_thread"] == [_event("socket.getaddrinfo", False, True)]
    assert probe["run_in_executor"] == [_event("socket.getaddrinfo", False, False)]


def test_a_blocking_client_is_audited_in_its_own_thread(probe: dict[str, Any]) -> None:
    assert probe["blocking_urlopen"] == [
        _event("socket.getaddrinfo", True, True),
        _event("socket.connect", True, True),
    ]
