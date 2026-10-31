"""The outbound guard (``egress_scan``) over the whole source tree, and proof it can fail.
The closure and the table themselves: ``test_egress_closure.py``."""

from pathlib import Path

import pytest

from egress_classified import TABLE
from egress_scan import (
    EGRESS_SUBMODULES,
    WORKER_ROLE_MODULES,
    imported_modules,
    scan_source,
    scan_tree,
    sources,
)
from egress_worker import WORKER_ENTRYPOINTS, WORKER_PACKAGES, loaded_watched_modules

SRC = Path(__file__).resolve().parent.parent / "src"
M = "ownvitals.withings.client"
WEB = "ownvitals.auth.routes"
NOT_CLASSIFIED = "can reach the network"


def plant(root: Path, module: str, source: str) -> None:
    *packages, name = module.split(".")
    directory = root.joinpath(*packages)
    directory.mkdir(parents=True, exist_ok=True)
    for depth in range(1, len(packages) + 1):
        init = root.joinpath(*packages[:depth], "__init__.py")
        if not init.exists():
            init.write_text("", encoding="utf-8")
    (directory / f"{name}.py").write_text(source, encoding="utf-8")


def test_no_module_reaches_the_network_outside_the_egress_package() -> None:
    assert scan_tree(SRC) == []


def test_the_guard_fails_when_a_planted_module_builds_its_own_client(tmp_path: Path) -> None:
    source = (
        "import httpx\n\nasync def fetch(url):\n"
        "    async with httpx.AsyncClient() as client:\n"
        "        return await client.get(url)\n"
    )
    plant(tmp_path, "ownvitals.withings.planted", source)
    found = scan_tree(tmp_path)
    assert {(m, line) for m, line, _ in found} == {
        ("ownvitals.withings.planted", 1),
        ("ownvitals.withings.planted", 4),
    }
    assert any(why.startswith(f"imports httpx: httpx {NOT_CLASSIFIED}") for _, _, why in found)


def test_the_guard_fails_when_a_planted_module_only_imports_a_network_name(
    tmp_path: Path,
) -> None:
    """The import alone is refused: deny by default, whatever the module does with it."""
    plant(tmp_path, "ownvitals.web.planted", "from ssl import create_connection\n")
    plant(tmp_path, "ownvitals.web.fine", "from urllib.parse import urlsplit\n")
    found = scan_tree(tmp_path)
    assert {(m, line) for m, line, _ in found} == {("ownvitals.web.planted", 1)}
    assert any(why.startswith(f"imports ssl: ssl {NOT_CLASSIFIED}") for _, _, why in found)


def test_the_planted_module_is_allowed_only_inside_the_egress_package(tmp_path: Path) -> None:
    plant(tmp_path, "ownvitals.egress.planted", "import httpx\nhttpx.AsyncClient()\n")
    assert scan_tree(tmp_path) == []
    assert scan_source("import httpx\nhttpx.AsyncClient()\n", "ownvitals.egressive") != []


def test_the_egress_package_itself_is_scanned_as_allowed() -> None:
    for name in ("__init__", *sorted(EGRESS_SUBMODULES)):
        source = (SRC / "ownvitals" / "egress" / f"{name}.py").read_text(encoding="utf-8")
        module = "ownvitals.egress" if name == "__init__" else f"ownvitals.egress.{name}"
        assert scan_source(source, module) == []
    opener = (SRC / "ownvitals" / "egress" / "opener.py").read_text(encoding="utf-8")
    assert scan_source(opener, "ownvitals.worker.loop") != []  # it does build clients


def test_the_submodules_are_found() -> None:
    assert {"core", "opener", "clients", "code_exchange"} <= EGRESS_SUBMODULES


REFUSED = [
    # urllib.request, http.client, socket, ssl in every spelling.
    "import urllib.request\nurllib.request.urlopen('https://x')",
    "from urllib import request\nrequest.urlopen('https://x')",
    "from urllib.request import urlopen as u\nu('https://x')",
    "import urllib.request as r\nr.build_opener()",
    "import urllib.request\nurllib.request.Request('https://x')",
    "import urllib\nurllib.request.urlopen('https://x')",
    "from http import client",
    "from http.client import HTTPSConnection",
    "import socket\nsocket.create_connection(('x', 443))",
    "import socket\nsocket.SocketType",
    "import socket\nexcept_types = (socket.timeout,)",
    "from ssl import create_connection",
    "from ssl import socket",
    "import ssl\nssl.get_server_certificate(('x', 443))",
    "import multiprocessing.connection\nmultiprocessing.connection.Client(('x', 1))",
    "import multiprocessing\nmultiprocessing.connection.Client(('x', 1))",
    "from multiprocessing.connection import Client",
    "from multiprocessing import connection",
    "from multiprocessing.managers import BaseManager",
    # httpx and the other client libraries, however little is used.
    "import httpx\nhttpx.AsyncClient()",
    "import httpx as h\nh.Client()",
    "from httpx import AsyncClient as C\nC(trust_env=False)",
    "import httpx\nx: httpx.AsyncBaseTransport\nhttpx.Timeout(1.0)",
    "import httpx\nclass T(httpx.AsyncBaseTransport): pass",
    "from httpx import HTTPError",
    "import httpcore",
    "import httpx2",
    "import requests",
    "import aiohttp",
    "from urllib3 import PoolManager",
    "import websockets\nwebsockets.connect('wss://x')",
    "from websockets.asyncio.client import connect",
    "import xmlrpc.client",
    "import imaplib",
    "import poplib",
    "import smtplib\nsmtplib.SMTP('x')",
    "from smtplib import SMTP_SSL as S\nclass M(S): pass",
    "import importlib\nimportlib.import_module('requests')",
    "import importlib\nimportlib.import_module('http.client')",
    "from importlib import import_module\nimport_module('httpx')",
    "__import__('aiohttp')",
    "import urllib.request\nclass H(urllib.request.BaseHandler): pass",
    # Authlib's network integrations, any of them, and the model SDKs, any alias.
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\nAsyncOAuth2Client()",
    "from authlib.integrations.httpx_client import OAuth2Client",
    "from authlib.integrations.httpx_client import AssertionClient",
    "from authlib.integrations.httpx_client import AsyncAssertionClient",
    "from authlib.integrations.starlette_client import OAuth\nOAuth().register('google')",
    "from authlib.integrations import starlette_client",
    "import authlib.integrations.requests_client",
    "import anthropic\nanthropic.AsyncAnthropic(api_key='k')",
    "from anthropic import AsyncAnthropic\nfrom ownvitals.egress import model_async_client\n"
    "AsyncAnthropic(http_client=model_async_client())",
    "from anthropic import AsyncClient",
    "from anthropic import Client",
    "from anthropic import AsyncAnthropicBedrock",
    "from anthropic import DefaultAsyncHttpxClient",
    "import openai\nopenai.AsyncOpenAI()",
    "from openai import AsyncClient",
    # asyncio's and anyio's connection functions, on whatever object.
    "import asyncio\nasyncio.open_connection('x', 443)",
    "from asyncio.streams import open_connection",
    "from asyncio import streams",
    "import asyncio\nasyncio.streams.open_connection('x', 443)",
    "import asyncio.streams\nasyncio.streams.StreamReader()",
    "loop.create_connection(factory, 'x', 443)",
    "async def f():\n    await loop.sock_connect(sock, ('x', 443))",
    "async def f():\n    await asyncio.get_running_loop().create_datagram_endpoint(f)",
    "import anyio\nanyio.connect_tcp('x', 443)",
    "from anyio import connect_unix",
    "import anyio\nawait anyio.create_udp_socket()",
    "import anyio\nanyio.to_thread.run_sync(f)",  # anyio is not classified at all
    "import multiprocessing\nmultiprocessing.cpu_count()",  # capable: classify it first
    "import subprocess\nsubprocess.run(['curl', 'x'])",  # select makes it capable
    "import uuid\nuuid.uuid1()",  # its getnode imports socket in a function
    # Star imports would hide what they bring in.
    "from ownvitals.egress.clients import *",
    "from os import *",
    # The lists are built and chosen in ownvitals.egress only.
    "from ownvitals.egress import Allowlist\nAllowlist(frozenset({'evil.example'}))",
    "from ownvitals import egress\negress.Allowlist(hosts)",
    "import ownvitals.egress.core as c\nc.Allowlist(hosts)",
    "import ownvitals.egress as e\ne.AllowlistTransport(e.WITHINGS_ALLOWLIST, inner)",
    "from ownvitals.egress.clients import AllowlistTransport\nAllowlistTransport(a, i)",
    "from ownvitals.egress import build_allowlist_opener\nbuild_allowlist_opener()",
    "from ownvitals.egress import DeadlineHTTPSHandler\nDeadlineHTTPSHandler()",
    "from ownvitals.egress.opener import DeadlineHTTPSConnection\nDeadlineHTTPSConnection('x')",
    "from ownvitals.egress import Allowlist\nclass Wider(Allowlist): pass",
    "from ownvitals.egress import AllowlistTransport\nclass T(AllowlistTransport): pass",
    # An entry point takes no list, by position, keyword or **.
    "from ownvitals.egress import withings_async_client, WORKER_ALLOWLIST\n"
    "withings_async_client(WORKER_ALLOWLIST)",
    "from ownvitals.egress import withings_async_client\nwithings_async_client(allowlist=a)",
    "from ownvitals.egress import model_async_client\nmodel_async_client(**options)",
    "from ownvitals.egress import google_async_client\ngoogle_async_client(allowlist=a)",
    "from ownvitals.egress import google_oauth_client\ngoogle_oauth_client(**kw)",
    "from ownvitals.egress.code_exchange import exchange_withings_code as x\nx(allowlist=a)",
    # Private names of the package and of mail, from outside them.
    "from ownvitals.egress.clients import _network_transport\n_network_transport()",
    "from ownvitals.egress import clients\nclients._google_signin_transport(10.0, None)",
    "from ownvitals.egress.code_exchange import _CodeExchangeTransport",
    "from ownvitals.egress import core\ncore._worker_list = None",
    "import ownvitals.egress.core\nownvitals.egress.core._install_worker_list(x)",
]


@pytest.mark.parametrize("source", REFUSED)
def test_the_guard_refuses(source: str) -> None:
    assert scan_source(source, M) != []


ALLOWED = [
    "from ownvitals.egress import withings_async_client\nwithings_async_client()",
    "from ownvitals.egress import withings_async_client\nwithings_async_client(total_seconds=5)",
    "from ownvitals import egress\negress.withings_async_client(total_seconds=t, inner=n)",
    "from ownvitals.egress import model_async_client\nmodel_async_client(timeout=1.0, refused=E)",
    "from ownvitals.egress import google_async_client\ngoogle_async_client(inner=t)",
    "from ownvitals.egress import google_oauth_client\n"
    "google_oauth_client(client_id=i, client_secret=s, scope='openid email', redirect_uri=u)",
    "from ownvitals.egress import exchange_withings_code\n"
    "async def f():\n"
    "    await exchange_withings_code(client_id=i, client_secret=s, code=c, redirect_uri=u)",
    "class Fine(Exception): pass",
    "from urllib.error import HTTPError, URLError",
    "from urllib.parse import urlsplit\nurlsplit('https://x')",
    "import asyncio\nasyncio.to_thread(f)\nasyncio.timeout(1.0)",
    "from authlib.oauth2.rfc6749 import OAuth2Error",
    "import os.path\nos.path.join('a', 'b')",
    "import json, re, logging, os, typing, datetime, dataclasses",
    "x = 'uvicorn.error'\ny = 'wbsapi.withings.net'",
    "from fastapi import FastAPI\nFastAPI()",
    "from sqlalchemy.ext.asyncio import create_async_engine",
    "import asyncio\nasyncio.run(main())",
    "loop.run_in_executor(None, f)\nloop.close()",
    "from ownvitals.egress import WITHINGS_ALLOWLIST\nWITHINGS_ALLOWLIST.check_url(u)",
    "from ownvitals.egress import OutboundBlocked, RequestDeadline, refuse_in_worker",
    "from ownvitals import egress\negress.__name__",
    "from ownvitals.mail import MailMessage, Mailer\nMailer(settings)",
    "def create_connection_string(): ...",
]


@pytest.mark.parametrize("source", ALLOWED)
def test_the_guard_allows(source: str) -> None:
    assert scan_source(source, M) == []


def test_a_relative_import_is_resolved() -> None:
    source = "from ..egress import Allowlist\nAllowlist(frozenset({'evil.example'}))"
    assert scan_source(source, "ownvitals.withings.client") != []
    private = "from ..egress.clients import _network_transport"
    assert scan_source(private, "ownvitals.withings.client") != []
    smtp = "from .. import mail\nmail.smtplib.SMTP('x')"
    assert scan_source(smtp, "ownvitals.auth.routes") != []


# Imports for type checking only: never at run time, so allowed; anywhere they could run, not.
TYPE_ONLY_ALLOWED = [
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx\n"
    "def f(c: httpx.AsyncClient) -> httpx.Response: ...",
    "import typing\nif typing.TYPE_CHECKING:\n    from ssl import SSLContext\n"
    "x: SSLContext | None = None",
    "from typing import TYPE_CHECKING as T\nif T:\n    from anthropic import AsyncAnthropic\n"
    "async def g(*, client: 'AsyncAnthropic') -> None: ...",
]
TYPE_ONLY_REFUSED = [
    # Used where it runs.
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx\nhttpx.AsyncClient()",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx\n"
    "def f(c=httpx.AsyncClient()) -> None: ...",
    "from typing import TYPE_CHECKING, cast\nif TYPE_CHECKING:\n    import socket\n"
    "cast(socket.socket, s)",
    # Not a type-checking block.
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    import httpx",
    "from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n    import httpx",
    "TYPE_CHECKING = True\nif TYPE_CHECKING:\n    import httpx",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING or x:\n    import httpx",
    "if __debug__:\n    import httpx",
]


@pytest.mark.parametrize("source", TYPE_ONLY_ALLOWED)
def test_a_type_checking_import_used_in_annotations_only_is_allowed(source: str) -> None:
    assert scan_source(source, M) == []


@pytest.mark.parametrize("source", TYPE_ONLY_REFUSED)
def test_a_type_checking_import_that_could_run_is_refused(source: str) -> None:
    assert scan_source(source, M) != []


# N3: a chain that ends in a network name or a private name is refused whatever its prefix.
CHAINS_REFUSED = [
    "import ownvitals.egress.clients\nownvitals.egress.clients.httpx.AsyncClient()",
    "from ownvitals.egress import clients\nclients.httpx.AsyncClient()",
    "from ownvitals import egress\negress.clients.httpx.AsyncClient()",
    "from ownvitals import egress\negress.opener.urllib.request.urlopen('https://x')",
    "from ownvitals import mail\nmail.smtplib.SMTP('x')",
    "import ownvitals.mail\nownvitals.mail.smtplib.SMTP_SSL('x')",
    "from ownvitals.mail import smtplib",
    "from ownvitals.egress.clients import httpx",
    "from ownvitals.egress.core import ssl",
    "from ownvitals.mail import _DeadlineSMTP\n_DeadlineSMTP('x', 25, deadline=d)",
    "from ownvitals import mail\nmail._DeadlineSMTPSSL('x', 465, deadline=d, context=c)",
    "import ownvitals.mail\nownvitals.mail._Deadline(0.0)",
    "from ownvitals.mail import _DeadlineReader",
    "from ownvitals import mail\nclass S(mail._DeadlineSMTP): pass",
]


@pytest.mark.parametrize("source", CHAINS_REFUSED)
def test_a_chain_through_a_module_that_holds_a_network_name_is_refused(source: str) -> None:
    assert scan_source(source, WEB) != []


def test_mail_may_use_its_own_private_names() -> None:
    source = "class _Deadline: ...\n_Deadline()\nimport smtplib\nsmtplib.SMTP('x')"
    assert scan_source(source, "ownvitals.mail") == []


# N2: only the worker's entry point and the probe CLI make a process the worker.
INSTALLS = [
    "from ownvitals.egress import install_worker_allowlist\ninstall_worker_allowlist()",
    "from ownvitals import egress\negress.install_worker_allowlist(ai_enabled=True)",
    "from ownvitals.egress.opener import install_worker_allowlist as i\ni()",
    "import ownvitals.egress\nownvitals.egress.install_worker_allowlist()",
    "from ownvitals.egress import install_worker_allowlist",
]


def test_the_worker_role_modules_are_the_entry_point_and_the_probe() -> None:
    assert set(WORKER_ROLE_MODULES) == {"ownvitals.worker.__main__", "ownvitals.withings.probe"}


@pytest.mark.parametrize("source", INSTALLS)
def test_only_the_worker_entry_points_install_the_worker_allowlist(source: str) -> None:
    for module in ("ownvitals.web", "ownvitals.worker", "ownvitals.worker.loop", "ownvitals.cli"):
        assert scan_source(source, module) != [], module
    for module in WORKER_ROLE_MODULES:
        assert scan_source(source, module) == [], module


def test_the_guard_fails_when_a_planted_web_module_installs_the_worker_list(
    tmp_path: Path,
) -> None:
    source = (
        "from ownvitals import egress\n\n\ndef start():\n    egress.install_worker_allowlist()\n"
    )
    plant(tmp_path, "ownvitals.web.startup", source)
    assert [(m, line) for m, line, _ in scan_tree(tmp_path)] == [("ownvitals.web.startup", 5)]
    (tmp_path / "ownvitals" / "web" / "startup.py").unlink()
    plant(tmp_path, "ownvitals.worker.__main__", source)
    assert scan_tree(tmp_path) == []


# ADR 0009: only the process entry points install the egress hook (web's in #77), and only the
# mail module opens an SMTP session's connection scope.
HOOK_INSTALLS = [
    "from ownvitals.egress import install_egress_hook\ninstall_egress_hook('web', s)",
    "from ownvitals import egress\negress.install_egress_hook('cli', s)",
    "from ownvitals.egress.hook import install_egress_hook as i\ni('worker', s)",
    "import ownvitals.egress\nownvitals.egress.install_egress_hook('worker', s)",
]
SMTP_SESSIONS = [
    "from ownvitals.egress import smtp_session\nwith smtp_session(): pass",
    "from ownvitals import egress\nwith egress.smtp_session(): pass",
    "from ownvitals.egress.hook import smtp_session as s\nwith s(): pass",
]


@pytest.mark.parametrize("source", HOOK_INSTALLS)
def test_only_the_process_entry_points_install_the_egress_hook(source: str) -> None:
    for module in ("ownvitals.worker.loop", "ownvitals.mail", WEB, "ownvitals.jobs.run", M):
        assert scan_source(source, module) != [], module
    for module in ("ownvitals.worker.__main__", "ownvitals.cli", "ownvitals.web"):
        assert scan_source(source, module) == [], module


@pytest.mark.parametrize("source", SMTP_SESSIONS)
def test_only_mail_opens_an_smtp_session(source: str) -> None:
    for module in ("ownvitals.cli", WEB, "ownvitals.worker.__main__", M):
        assert scan_source(source, module) != [], module
    assert scan_source(source, "ownvitals.mail") == []


def test_the_connection_scope_is_private_to_the_egress_package() -> None:
    source = "from ownvitals.egress.hook import _connection_scope\nwith c('x'): pass"
    assert scan_source(source, "ownvitals.mail") != []
    assert scan_source(source, "ownvitals.egress.clients") == []


# The one exemption: account mail over smtplib (roadmap Amendment 1).
def test_mail_is_the_only_module_allowed_a_connection_of_its_own() -> None:
    limited = {key: entry.only_in for key, entry in TABLE.items() if entry.only_in}
    # The other limited entry is no connection: the worker's lease owner name (a local call).
    assert limited == {"smtplib": ("ownvitals.mail",), "socket": ("ownvitals.worker.loop",)}
    assert TABLE["socket"].names == {"gethostname"}
    assert not TABLE["socket"].exceptions
    assert TABLE["smtplib"].names == {"SMTP", "SMTP_SSL"}
    assert TABLE["smtplib"].exceptions


def test_an_exemption_is_per_module_and_per_name() -> None:
    """N12: inside mail, only the two clients and smtplib's exception classes."""
    call = "import smtplib\nsmtplib.SMTP('x')"
    assert scan_source(call, "ownvitals.mail") == []
    assert scan_source(call, WEB) != []
    assert scan_source(call, "ownvitals.mail.extra") != []
    for other in (
        "import smtplib\nsmtplib.LMTP('x')",
        "from smtplib import LMTP\nclass L(LMTP): ...",
        "import httpx\nhttpx.AsyncClient()",
        "import socket",
        "import ssl\nssl.create_default_context()",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import ssl\nssl.SSLContext()",
        "import smtplib\nsmtplib.socket.socket()",
        "import smtplib\nsmtplib.ssl.create_default_context()",
        "from smtplib import socket",
        "import smtplib\nsmtplib.SMTP_PORT",
    ):
        assert scan_source(other, "ownvitals.mail") != [], other
    errors = "import smtplib\nraise smtplib.SMTPDataError(1, 'x')\nsmtplib.SMTPException"
    assert scan_source(errors, "ownvitals.mail") == []
    subclass = "import smtplib\nclass Deadline(smtplib.SMTP_SSL): ..."
    assert scan_source(subclass, "ownvitals.mail") == []
    assert scan_source(subclass, WEB) != []


def test_the_withings_and_job_packages_count_as_worker_code() -> None:
    """PLAN §2: the worker owns every Withings call but the code exchange; none of it may import
    the web-only mail module."""
    assert {"ownvitals.withings", "ownvitals.jobs", "ownvitals.worker"} <= set(WORKER_PACKAGES)


def test_worker_code_never_imports_mail() -> None:
    for module, source in sources(SRC):
        if not module.startswith(WORKER_PACKAGES):
            continue
        imported = imported_modules(source, module)
        assert not any(n == "ownvitals.mail" or n.startswith("ownvitals.mail.") for n in imported)


def test_the_worker_process_never_loads_mail_or_another_client_library() -> None:
    """Reachability, not only direct imports: a fresh interpreter imports every worker entry
    point; neither mail nor a client library the worker does not use may be loaded."""
    assert loaded_watched_modules(WORKER_ENTRYPOINTS) == []


def test_the_reachability_check_fails_when_a_worker_path_reaches_mail() -> None:
    assert loaded_watched_modules((*WORKER_ENTRYPOINTS, "ownvitals.mail")) != []
