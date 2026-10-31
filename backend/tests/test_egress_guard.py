"""The outbound guard (``egress_scan``) over the whole source tree, and proof it can fail."""

from pathlib import Path

import pytest

from egress_scan import (
    EGRESS_SUBMODULES,
    EXEMPT,
    WORKER_ENTRYPOINTS,
    WORKER_PACKAGES,
    exempt_calls_used,
    imported_modules,
    loaded_exempt_modules,
    scan_source,
    scan_tree,
    sources,
)

SRC = Path(__file__).resolve().parent.parent / "src"


def test_no_module_builds_an_outbound_client_outside_the_egress_package() -> None:
    assert scan_tree(SRC) == []


def test_the_guard_fails_when_a_planted_module_builds_its_own_client(tmp_path: Path) -> None:
    package = tmp_path / "ownvitals"
    (package / "withings").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "withings" / "__init__.py").write_text("", encoding="utf-8")
    (package / "withings" / "planted.py").write_text(
        "import httpx\n\nasync def fetch(url):\n"
        "    async with httpx.AsyncClient() as client:\n"
        "        return await client.get(url)\n",
        encoding="utf-8",
    )
    assert scan_tree(tmp_path) == [
        (
            "ownvitals.withings.planted",
            4,
            "calls httpx.AsyncClient: outbound clients live in ownvitals.egress",
        )
    ]


def test_the_planted_module_is_allowed_only_inside_the_egress_package(tmp_path: Path) -> None:
    package = tmp_path / "ownvitals" / "egress"
    package.mkdir(parents=True)
    (package / "planted.py").write_text("import httpx\nhttpx.AsyncClient()\n", encoding="utf-8")
    assert scan_tree(tmp_path) == []
    assert scan_source("import httpx\nhttpx.AsyncClient()\n", "ownvitals.egressive") != []


def test_every_exemption_is_still_used() -> None:
    found = dict(sources(SRC))
    for module, calls in EXEMPT.items():
        assert module in found, f"{module} no longer exists; drop its exemption"
        assert exempt_calls_used(found[module], module) == set(calls), module


def test_mail_is_the_only_module_allowed_a_connection_of_its_own() -> None:
    """Roadmap Amendment 1: SMTP is the one guard-listed web-only site."""
    assert set(EXEMPT) == {"ownvitals.mail"}
    assert set(EXEMPT["ownvitals.mail"]) == {"smtplib.SMTP", "smtplib.SMTP_SSL"}


def test_worker_code_never_imports_an_exempt_module() -> None:
    for module, source in sources(SRC):
        if not module.startswith(WORKER_PACKAGES):
            continue
        imported = imported_modules(source, module)
        for exempt in EXEMPT:
            assert not any(n == exempt or n.startswith(exempt + ".") for n in imported), (
                module,
                exempt,
            )


def test_the_worker_process_never_loads_an_exempt_module() -> None:
    """Reachability, not only direct imports: a fresh interpreter imports every worker entry
    point; no exempt module (mail) or other client library may be loaded."""
    assert loaded_exempt_modules(WORKER_ENTRYPOINTS) == []


def test_the_reachability_check_fails_when_a_worker_path_reaches_an_exempt_module() -> None:
    assert loaded_exempt_modules((*WORKER_ENTRYPOINTS, "ownvitals.mail")) != []


def test_the_egress_package_itself_is_scanned_as_allowed() -> None:
    for name in ("__init__", *sorted(EGRESS_SUBMODULES)):
        source = (SRC / "ownvitals" / "egress" / f"{name}.py").read_text(encoding="utf-8")
        module = "ownvitals.egress" if name == "__init__" else f"ownvitals.egress.{name}"
        assert scan_source(source, module) == []
    opener = (SRC / "ownvitals" / "egress" / "opener.py").read_text(encoding="utf-8")
    assert scan_source(opener, "ownvitals.worker.loop") != []  # it does build clients


def test_the_submodules_are_found() -> None:
    assert {"core", "opener", "clients", "code_exchange"} <= EGRESS_SUBMODULES


M = "ownvitals.withings.client"

REFUSED = [
    "import urllib.request\nurllib.request.urlopen('https://x')",
    "from urllib import request\nrequest.urlopen('https://x')",
    "from urllib.request import urlopen as u\nu('https://x')",
    "import urllib.request as r\nr.build_opener()",
    "import urllib.request\nurllib.request.install_opener(None)",
    "from urllib.request import OpenerDirector\nOpenerDirector()",
    "from urllib.request import HTTPSHandler\nHTTPSHandler()",
    "import httpx\nhttpx.AsyncClient()",
    "import httpx as h\nh.Client()",
    "from httpx import AsyncClient as C\nC(trust_env=False)",
    "import httpx\nhttpx.AsyncHTTPTransport()",
    "import httpx\nhttpx.get('https://x')",
    "import httpx\nhttpx.AsyncClient(transport=t, trust_env=False)",
    "import socket\nsocket.create_connection(('x', 443))",
    "import socket\nsocket.socket()",
    "import smtplib\nsmtplib.SMTP('x')",
    "import asyncio\nasyncio.open_connection('x', 443)",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\nAsyncOAuth2Client()",
    "from authlib.integrations.httpx_client import OAuth2Client\nOAuth2Client()",
    "import anthropic\nanthropic.AsyncAnthropic(api_key='k')",
    "from anthropic import AsyncAnthropic\nAsyncAnthropic()",
    "from anthropic import Anthropic\nAnthropic(http_client=c)",
    "from anthropic import DefaultAsyncHttpxClient\nDefaultAsyncHttpxClient()",
    "import openai\nopenai.AsyncOpenAI()",
    "import requests",
    "import aiohttp",
    "from urllib3 import PoolManager",
    "import http.client",
    "from http import client",
    "from http.client import HTTPSConnection",
    "import httpcore",
    "import httpx2",
    "import importlib\nimportlib.import_module('requests')",
    "__import__('aiohttp')",
    "from anthropic import AsyncAnthropic\nAsyncAnthropic(http_client=None)",
    "from anthropic import AsyncAnthropic\nAsyncAnthropic(http_client=self.client)",
    "import anthropic, httpx\nanthropic.AsyncAnthropic(http_client=httpx.AsyncClient())",
    "import xmlrpc.client",
    "import imaplib",
    "import poplib",
    "import ssl\nssl.get_server_certificate(('x', 443))",
    "loop.create_connection(factory, 'x', 443)",
    "async def f():\n    await loop.sock_connect(sock, ('x', 443))",
    "async def f():\n    await asyncio.get_running_loop().create_connection(f, 'x', 443)",
    "import httpx\nclass C(httpx.AsyncClient): pass",
    "from httpx import Client\nclass C(Client): pass",
    "import httpx\nclass T(httpx.AsyncBaseTransport): pass",
    "import urllib.request\nclass H(urllib.request.BaseHandler): pass",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "class C(AsyncOAuth2Client): ...",
    # The lists are built and chosen in ownvitals.egress only.
    "from ownvitals.egress import Allowlist\nAllowlist(frozenset({'evil.example'}))",
    "from ownvitals import egress\negress.Allowlist(hosts)",
    "from ownvitals.egress.core import Allowlist\nAllowlist(frozenset({'evil.example'}))",
    "import ownvitals.egress.core as c\nc.Allowlist(hosts)",
    "import ownvitals.egress as e\ne.AllowlistTransport(e.WITHINGS_ALLOWLIST, inner)",
    "from ownvitals.egress.clients import AllowlistTransport\nAllowlistTransport(a, i)",
    "from ownvitals.egress import build_allowlist_opener\nbuild_allowlist_opener()",
    "from ownvitals.egress import DeadlineHTTPSHandler\nDeadlineHTTPSHandler()",
    "from ownvitals.egress.opener import DeadlineHTTPSConnection\nDeadlineHTTPSConnection('x')",
    "from ownvitals.egress import Allowlist\nclass Wider(Allowlist): pass",
    "from ownvitals.egress import AllowlistTransport\nclass T(AllowlistTransport): pass",
    # A client builder takes no list, by position, keyword or **.
    "from ownvitals.egress import withings_async_client, WORKER_ALLOWLIST\n"
    "withings_async_client(WORKER_ALLOWLIST)",
    "from ownvitals.egress import withings_async_client\nwithings_async_client(allowlist=a)",
    "from ownvitals.egress import model_async_client\nmodel_async_client(**options)",
    "from ownvitals.egress import google_async_client\ngoogle_async_client(allowlist=a)",
    "from ownvitals.egress import withings_code_exchange_client\n"
    "withings_code_exchange_client(allowlist=a)",
    "from ownvitals.egress.code_exchange import withings_code_exchange_client as w\nw(**kw)",
    # Private names of the package, from outside it.
    "from ownvitals.egress.clients import _network_transport\n_network_transport()",
    "from ownvitals.egress import clients\nclients._network_transport()",
    "from ownvitals.egress.code_exchange import _CodeExchangeTransport",
    "from ownvitals.egress import core\ncore._worker_list = None",
    "import ownvitals.egress.core\nownvitals.egress.core._install_worker_list(x)",
    # Authlib's client only with the package's Google transport and trust_env=False.
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "AsyncOAuth2Client(transport=t, trust_env=False)",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "from ownvitals.egress import google_signin_transport\n"
    "AsyncOAuth2Client(transport=google_signin_transport())",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "from ownvitals.egress import google_signin_transport\n"
    "AsyncOAuth2Client(transport=google_signin_transport(), trust_env=True)",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "from ownvitals.egress import google_signin_transport\n"
    "AsyncOAuth2Client(transport=google_signin_transport(), trust_env=False, proxy=p)",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "from ownvitals.egress import google_signin_transport\n"
    "AsyncOAuth2Client(transport=google_signin_transport(), trust_env=False, mounts=m)",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "from ownvitals.egress import google_signin_transport\n"
    "AsyncOAuth2Client(transport=google_signin_transport(), trust_env=False, **extra)",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "from ownvitals.egress import withings_async_client\n"
    "AsyncOAuth2Client(transport=withings_async_client(), trust_env=False)",
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
    "from ownvitals.egress.code_exchange import withings_code_exchange_client\n"
    "withings_code_exchange_client(inner=runtime.transport)",
    "from ownvitals.egress import install_worker_allowlist\n"
    "install_worker_allowlist(ai_enabled=settings.ai_insights)",
    "import httpx\nx: httpx.AsyncBaseTransport\nhttpx.Timeout(1.0)\nhttpx.MockTransport(f)",
    "import anthropic\nanthropic.AsyncAnthropic(api_key=k, http_client=c)",
    "from anthropic import AsyncAnthropic\nfrom ownvitals.egress import model_async_client\n"
    "AsyncAnthropic(http_client=model_async_client())",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "from ownvitals.egress import google_signin_transport\n"
    "AsyncOAuth2Client(client_id=i, client_secret=s, scope='openid email',\n"
    "    transport=google_signin_transport(inner=runtime.transport), trust_env=False)",
    "from authlib.integrations.httpx_client import AsyncOAuth2Client\n"
    "from ownvitals.egress.clients import google_signin_transport\n"
    "AsyncOAuth2Client(transport=google_signin_transport(), trust_env=False)",
    "class Fine(Exception): pass",
    "import socket\nexcept_types = (socket.timeout,)",
    "from urllib.error import HTTPError, URLError",
    "from urllib.parse import urlsplit\nurlsplit('https://x')",
    "import urllib.request\nurllib.request.Request('https://x')",
    "from ownvitals.egress import WITHINGS_ALLOWLIST\nWITHINGS_ALLOWLIST.check_url(u)",
    "from ownvitals.egress import OutboundBlocked, RequestDeadline, refuse_in_worker",
    "from ownvitals import egress\negress.__name__",
]


@pytest.mark.parametrize("source", ALLOWED)
def test_the_guard_allows(source: str) -> None:
    assert scan_source(source, M) == []


def test_a_relative_import_of_an_allowlist_is_refused_too() -> None:
    source = "from ..egress import Allowlist\nAllowlist(frozenset({'evil.example'}))"
    assert scan_source(source, "ownvitals.withings.client") != []
    private = "from ..egress.clients import _network_transport"
    assert scan_source(private, "ownvitals.withings.client") != []


def test_an_exemption_is_per_module_and_per_call() -> None:
    call = "import smtplib\nsmtplib.SMTP('x')"
    assert scan_source(call, "ownvitals.mail") == []
    assert scan_source(call, "ownvitals.auth.routes") != []
    other = "import smtplib\nsmtplib.LMTP('x')"
    assert scan_source(other, "ownvitals.mail") != []
    client = "import httpx\nhttpx.AsyncClient()"
    assert scan_source(client, "ownvitals.mail") != []


def test_the_withings_and_job_packages_count_as_worker_code() -> None:
    """PLAN §2: the worker owns every Withings call but the code exchange; none of it may import
    the web-only mail module."""
    assert {"ownvitals.withings", "ownvitals.jobs", "ownvitals.worker"} <= set(WORKER_PACKAGES)
