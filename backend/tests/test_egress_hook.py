"""ADR 0009's egress hook. The hook cannot be removed, so its behaviour is tested in fresh
interpreters (``egress_hook_child.py``, one per process kind, all cases in one run); the
functions around it (the policy, the loop check, the entry points' installs) are tested here,
where ``conftest._egress_hook_stays_out`` keeps the real hook out."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ownvitals import cli, mail
from ownvitals.config import Settings
from ownvitals.egress import (
    EgressRefused,
    install_egress_hook,
    installed_kind,
    require_standard_event_loop,
)
from ownvitals.egress.hook import WEB_HOSTS, _policy_for  # pyright: ignore[reportPrivateUsage]
from ownvitals.worker import __main__ as worker_main

CHILD = Path(__file__).resolve().parent / "egress_hook_child.py"
KINDS = ("web", "worker", "cli")
LINUX = sys.platform.startswith("linux")
OUTSIDE = "example.invalid"
GETADDRINFO, CONNECT = "socket.getaddrinfo", "socket.connect"


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Each kind's child, run in parallel; no ``OWNVITALS_*`` setting of the machine leaks in."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OWNVITALS_")}
    env["OWNVITALS_SECRETS_DIR"] = str(tmp_path_factory.mktemp("child-secrets"))
    children = {
        kind: subprocess.Popen(  # noqa: S603 - our own interpreter and script, fixed arguments
            [sys.executable, str(CHILD), kind],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for kind in KINDS
    }
    results: dict[str, dict[str, Any]] = {}
    for kind, child in children.items():
        out, err = child.communicate(timeout=180)
        assert child.returncode == 0, err[-3000:]
        results[kind] = json.loads(out.strip().splitlines()[-1])
    return results


def case(runs: dict[str, dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return runs[kind]["results"][name]


# --- refused ------------------------------------------------------------------------------------

REFUSED_EVERYWHERE = {
    "resolve_outside": (GETADDRINFO, OUTSIDE),
    "gethostbyname_outside": ("socket.gethostbyname", OUTSIDE),
    "gethostbyname_ex_outside": ("socket.gethostbyname", OUTSIDE),
    "gethostbyaddr": ("socket.gethostbyaddr", "reverse lookup"),
    "getnameinfo": ("socket.getnameinfo", "reverse lookup"),
    "fresh_opener": (GETADDRINFO, OUTSIDE),  # a fresh urllib opener, as minidom builds one
    "create_connection": (GETADDRINFO, OUTSIDE),
    "minidom_parse_uri": (GETADDRINFO, OUTSIDE),
    "connect_outside_scope": (CONNECT, "AF_INET"),  # to what an allowed name resolved to
    "connect_ex_outside_scope": (CONNECT, "AF_INET"),
    "udp_sendto": ("socket.sendto", "AF_INET"),
}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", sorted(REFUSED_EVERYWHERE))
def test_refused_in_every_process(runs: dict[str, dict[str, Any]], kind: str, name: str) -> None:
    result = case(runs, kind, name)
    event, target = REFUSED_EVERYWHERE[name]
    assert [event, target, kind] in result["refused"], result
    assert result["error"] is not None
    assert result["accepted"] == 0


@pytest.mark.parametrize("kind", ["web", "cli"])
def test_numpy_fetching_a_url_is_refused(runs: dict[str, dict[str, Any]], kind: str) -> None:
    """``np.loadtxt(url)``: numpy's lazy ``DataSource`` calls ``urlopen``; the hook refuses its
    lookup (numpy then reports the file as not found)."""
    result = case(runs, kind, "numpy_loadtxt")
    assert [GETADDRINFO, OUTSIDE, kind] in result["refused"]
    assert (result["error"], result["accepted"]) == ("FileNotFoundError", 0)


def test_numpy_fetching_a_url_in_the_worker_stops_at_its_opener(
    runs: dict[str, dict[str, Any]],
) -> None:
    result = case(runs, "worker", "numpy_loadtxt")
    assert (result["error"], result["accepted"]) == ("OutboundBlocked", 0)


@pytest.mark.parametrize(
    ("kind", "name", "event", "target"),
    [
        ("worker", "model_host_without_ai", GETADDRINFO, "api.anthropic.com"),
        ("worker", "smtp", GETADDRINFO, "mailpit"),  # inside an SMTP session, still refused
        ("cli", "worker_host_before_probe", GETADDRINFO, "wbsapi.withings.net"),
        *[(k, "unix_other", CONNECT, "AF_UNIX") for k in KINDS if LINUX],
    ],
)
def test_refused_in_one_process(
    runs: dict[str, dict[str, Any]], kind: str, name: str, event: str, target: str
) -> None:
    result = case(runs, kind, name)
    assert [event, target, kind] in result["refused"], result
    assert result["error"] == "EgressRefused"


# --- allowed ------------------------------------------------------------------------------------

ALLOWED_EVERYWHERE = [
    "database_resolves",
    "database_resolves_in_executor",  # asyncio's executor thread: no context needed
    "standard_loop",  # a selector loop starts (Windows: its socketpair connects)
    "second_install_same_kind",
    *(["unix_database"] if LINUX else []),
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", ALLOWED_EVERYWHERE)
def test_allowed_in_every_process(runs: dict[str, dict[str, Any]], kind: str, name: str) -> None:
    result = case(runs, kind, name)
    assert (result["error"], result["refused"]) == (None, []), result


@pytest.mark.parametrize("kind", ["web", "worker"])
def test_the_egress_client_resolves_and_connects(
    runs: dict[str, dict[str, Any]], kind: str
) -> None:
    """The allowlisted name resolves (to 127.0.0.1, through the fake resolver) and the transport
    connects inside its scope: the operating system refuses port 443, not the hook."""
    result = case(runs, kind, "egress_client")
    assert result["refused"] == []
    assert "EgressRefused" not in result["chain"]
    assert "ConnectionRefusedError" in result["chain"], result


def test_the_worker_urllib_opener_connects(runs: dict[str, dict[str, Any]]) -> None:
    result = case(runs, "worker", "urllib_opener")
    assert (result["refused"], result["accepted"]) == ([], 1), result


@pytest.mark.parametrize("kind", ["web", "cli"])
def test_mail_delivers_inside_its_session(runs: dict[str, dict[str, Any]], kind: str) -> None:
    result = case(runs, kind, "smtp")
    assert (result["error"], result["refused"]) == (None, [])
    assert result["extra"] == {"messages": 1, "ehlo": ["vitals.example.org"]}


def test_the_cli_resolves_worker_hosts_once_the_probe_installs_the_worker_list(
    runs: dict[str, dict[str, Any]],
) -> None:
    result = case(runs, "cli", "worker_host_after_probe")
    assert (result["error"], result["refused"]) == (None, [])


# --- the hook itself ----------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_the_hook_is_installed_once_per_process(runs: dict[str, dict[str, Any]], kind: str) -> None:
    assert case(runs, kind, "second_install_other_kind")["error"] == "RuntimeError"
    assert "uvloop" in (case(runs, kind, "uvloop_importable")["error"] or "")


@pytest.mark.parametrize("kind", KINDS)
def test_a_refusal_logs_one_redacted_line(runs: dict[str, dict[str, Any]], kind: str) -> None:
    """The event, the host or the family and the process kind: never a path, a query or a
    payload (``fresh_opener`` asked for ``/secret-path/report?token=abc123``)."""
    log = runs[kind]["log"]
    assert all(secret not in log for secret in ("secret-path", "report", "token", "abc123"))
    lines = [json.loads(line) for line in log.splitlines() if line.strip()]
    refusals = [line for line in lines if line.get("message") == "outbound connection refused"]
    assert len(refusals) >= len(REFUSED_EVERYWHERE)
    assert all(line["process_kind"] == kind for line in refusals)
    assert all(set(line) >= {"event", "target"} for line in refusals)


def test_a_refusal_is_a_permission_error() -> None:
    refused = EgressRefused("socket.connect", "AF_INET")
    assert isinstance(refused, PermissionError)
    assert str(refused) == "egress refused: socket.connect (AF_INET)"


# --- the policy ---------------------------------------------------------------------------------


def _settings(**values: Any) -> Settings:
    return Settings(
        database_url=SecretStr(
            "postgresql+psycopg://ownvitals_app@DB.Example.Test.:5432/ownvitals"
        ),
        maint_database_url=SecretStr(
            "postgresql+psycopg://ownvitals_maint@/ownvitals?host=/run/pg"
        ),
        smtp_host="Mail.Example.Org",
        **values,
    )


def test_each_kind_resolves_its_own_hosts() -> None:
    web, worker, cli_ = (_policy_for(k, _settings()) for k in KINDS)
    database = {"db.example.test", "127.0.0.1"}  # the app's and the migrations' URLs
    assert web.hosts == WEB_HOSTS | database | {"mail.example.org"}
    assert worker.hosts == frozenset(database)  # plus the installed worker list, at run time
    assert cli_.hosts == frozenset(database | {"mail.example.org"})
    assert web.unix_paths == worker.unix_paths == frozenset({"/run/pg/.s.PGSQL.5432"})


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown process kind"):
        install_egress_hook("browser", Settings())  # type: ignore[arg-type]
    assert installed_kind() is None


# --- the event loop (ADR 0009: only the selector loop's sockets are audited) --------------------


class ForeignLoop(asyncio.SelectorEventLoop):
    """A selector loop from outside the standard library, as uvloop's would be."""


def test_the_standard_selector_loop_is_accepted() -> None:
    async def check() -> None:
        require_standard_event_loop()

    asyncio.run(check(), loop_factory=asyncio.SelectorEventLoop)


@pytest.mark.parametrize(
    "factory",
    [ForeignLoop, *([asyncio.ProactorEventLoop] if sys.platform == "win32" else [])],  # pyright: ignore[reportAttributeAccessIssue]
)
def test_another_loop_is_refused(factory: Any) -> None:
    async def check() -> None:
        require_standard_event_loop()

    with pytest.raises(RuntimeError, match="refusing to run on"):
        asyncio.run(check(), loop_factory=factory)


def test_the_worker_installs_the_hook_and_refuses_another_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []
    original = worker_main._run  # pyright: ignore[reportPrivateUsage]

    async def run(settings: Settings) -> None:
        seen.append(installed_kind())

    def no_engine(*_: object) -> None:
        raise AssertionError("the worker went on past the loop check")

    monkeypatch.setattr(worker_main, "_run", run)
    assert worker_main.main() == 0
    assert seen == ["worker"]  # installed before the loop ran anything
    monkeypatch.setattr(worker_main, "create_worker_engine", no_engine)
    with pytest.raises(RuntimeError, match="refusing to run on"):
        asyncio.run(original(Settings()), loop_factory=ForeignLoop)  # before any connection


def test_the_cli_installs_the_hook_and_refuses_another_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse
    import io

    seen: list[str | None] = []

    async def hello(*_: object) -> int:
        seen.append(installed_kind())
        return 0

    monkeypatch.setitem(cli.COMMANDS, "hello", hello)
    args = argparse.Namespace(command="hello")
    assert cli.run_command(args, Settings(), io.StringIO(), io.StringIO()) == 0
    assert seen == ["cli"]
    monkeypatch.setattr(cli, "event_loop_factory", lambda: ForeignLoop)
    with pytest.raises(RuntimeError, match="refusing to run on"):
        cli.run_command(args, Settings(), io.StringIO(), io.StringIO())
    assert seen == ["cli"]  # the command never ran


# --- mail never looks its own name up ---------------------------------------------------------


@pytest.mark.parametrize(
    ("public", "expected"),
    [
        ("https://vitals.example.org", "vitals.example.org"),
        ("https://vitals.example.org.:8443/app", "vitals.example.org"),
        ("http://127.0.0.1:8000", mail.FALLBACK_EHLO_NAME),
        ("http://[::1]:8000", mail.FALLBACK_EHLO_NAME),
        ("http://localhost:8000", mail.FALLBACK_EHLO_NAME),
    ],
)
def test_the_ehlo_name_comes_from_the_public_url(public: str, expected: str) -> None:
    assert mail.ehlo_name(public) == expected


async def test_mail_never_calls_getfqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    """smtplib's default ``local_hostname`` is ``socket.getfqdn()``: a reverse lookup outside
    the deadline that names the machine to the server."""
    import socket

    from smtpfake import SmtpFake

    def no_lookup(*_: object) -> str:
        raise AssertionError("getfqdn called")

    monkeypatch.setattr(socket, "getfqdn", no_lookup)  # smtplib calls socket.getfqdn
    async with SmtpFake() as smtp:
        values = {"smtp_port": smtp.port, "smtp_tls": "none", "smtp_host": "127.0.0.1"}
        settings = Settings(public_base_url="https://vitals.example.org", **values)
        await mail.Mailer(settings).send(mail.MailMessage("to@example.com", "Hi", "Synthetic.\n"))
    assert smtp.ehlo_names == ["vitals.example.org"]


# --- no uvloop (ADR 0009) -----------------------------------------------------------------------

LOCKFILE = Path(__file__).resolve().parents[1] / "uv.lock"


def test_uvloop_is_neither_locked_nor_importable() -> None:
    """uvloop's sockets raise no audit events, so the hook would not see them: it must not be a
    dependency (``uvicorn[standard]`` pulls it in) or installed."""
    packages = tomllib.loads(LOCKFILE.read_text(encoding="utf-8"))["package"]
    assert "uvloop" not in {package["name"] for package in packages}
    assert importlib.util.find_spec("uvloop") is None
