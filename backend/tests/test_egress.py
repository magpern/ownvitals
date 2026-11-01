"""The outbound lists (PLAN §2): the worker's urllib opener and the httpx clients. No test opens a
connection: the network handler (urllib) and transport (httpx) are replaced by fakes that record
what reached them."""

import io
import logging
import ssl
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpx
import pytest

from ownvitals import egress
from ownvitals.egress import (
    WORKER_ALLOWLIST,
    Allowlist,
    OutboundBlocked,
    build_allowlist_opener,
    clients,
    core,
    install_worker_allowlist,
    model_async_client,
    withings_async_client,
)

PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY")
API = "https://wbsapi.withings.net"


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that makes this process the worker hands neither the opener nor the flag on."""
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


@pytest.fixture
def worker() -> None:
    install_worker_allowlist(ai_enabled=True)


def _field(obj: object, name: str) -> Any:
    """A stdlib attribute the type stubs leave out."""
    return getattr(obj, name)


class FakeHTTPS(urllib.request.BaseHandler):
    """Answers ``https`` without a network: 200, or a redirect from ``redirects`` by URL."""

    def __init__(self, redirects: dict[str, str] | None = None, status: int = 200) -> None:
        self.seen: list[tuple[str, str, bool]] = []
        self.redirects = redirects or {}
        self.status = status

    def https_open(self, req: urllib.request.Request) -> Any:
        self.seen.append((req.full_url, req.host, req.has_proxy()))
        headers = Message()
        status = self.status
        if req.full_url in self.redirects:
            status = 302
            headers["Location"] = self.redirects[req.full_url]
        response = urllib.response.addinfourl(
            io.BytesIO(b'{"status": 0}'), headers, req.full_url, status
        )
        response.msg = "fake"  # pyright: ignore[reportAttributeAccessIssue]
        return response


def _opener(fake: FakeHTTPS) -> urllib.request.OpenerDirector:
    # The worker's list with AI enabled, its widest.
    return build_allowlist_opener(egress.worker_allowlist(ai_enabled=True), https_handler=fake)


@pytest.mark.parametrize(
    "url",
    [
        f"{API}/measure?action=getmeas",
        "https://WBSAPI.WITHINGS.NET/v2/user",
        "https://wbsapi.withings.net:443/v2/user",
        "https://account.withings.com/oauth2_user/authorize2",
        "https://api.anthropic.com/v1/messages",
    ],
)
def test_https_to_a_listed_host_goes_out(url: str) -> None:
    fake = FakeHTTPS()
    with _opener(fake).open(url, timeout=1) as response:
        assert response.read() == b'{"status": 0}'
    assert [u for u, _, _ in fake.seen] == [url]


BLOCKED = [
    ("http://wbsapi.withings.net/v2/user", "wbsapi.withings.net"),
    ("https://wbsapi.withings.net:8443/v2/user", "wbsapi.withings.net"),
    ("https://evil.example/v1", "evil.example"),
    ("https://wbsapi.withings.net.evil.example/", "wbsapi.withings.net.evil.example"),
    ("https://wbsapi.withings.net./v2/user", "wbsapi.withings.net."),
    ("https://user:pw@wbsapi.withings.net/v2", "wbsapi.withings.net"),
    ("https://wbsapi.withings.net@evil.example/v2", "evil.example"),
    ("https://127.0.0.1/", "127.0.0.1"),
    ("https://[::1]/", "::1"),
    ("https://169.254.169.254/latest/meta-data", "169.254.169.254"),
    ("ftp://wbsapi.withings.net/x", "wbsapi.withings.net"),
    ("file:///etc/passwd", ""),
    ("data:text/plain,hello", ""),
    ("https://wbsapi.withings.net:99999/", ""),
    ("https://withings.net/v2/user", "withings.net"),
    ("https://scalews.withings.com/cgi-bin/measure", "scalews.withings.com"),  # D1: private API
    ("https://healthmate.withings.com/", "healthmate.withings.com"),
    ("http://account.withings.com/oauth2/token", "account.withings.com"),
    ("https://oauth2.googleapis.com/token", "oauth2.googleapis.com"),  # web only
    ("https://api.anthropic.com.evil.example/v1", "api.anthropic.com.evil.example"),
]


@pytest.mark.parametrize(("url", "host"), BLOCKED)
def test_anything_else_is_refused_before_a_connection(url: str, host: str) -> None:
    fake = FakeHTTPS()
    with pytest.raises(OutboundBlocked) as raised:
        _opener(fake).open(url, timeout=1)
    assert raised.value.host == host
    assert raised.value.args == ("outbound request blocked",)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None  # no ValueError holding URL parts
    assert fake.seen == []


def test_a_refusal_logs_the_host_only(caplog: pytest.LogCaptureFixture) -> None:
    url = "https://evil.example/path/secret-path?token=planted-query#frag"
    opener = _opener(FakeHTTPS())
    with caplog.at_level(logging.WARNING, "ownvitals.egress"), pytest.raises(OutboundBlocked):
        opener.open(url, timeout=1)
    [record] = caplog.records
    assert record.getMessage() == "outbound request blocked"
    assert record.__dict__["host"] == "evil.example"
    assert "secret-path" not in repr(record.__dict__)
    assert "planted-query" not in repr(record.__dict__)


def test_a_logged_host_is_printable_and_bounded() -> None:
    with pytest.raises(OutboundBlocked) as raised:
        WORKER_ALLOWLIST.check("https", "EVIL\n.example" + "a" * 400, None, False)
    assert raised.value.host.startswith("evil?.example")
    assert len(raised.value.host) == 253


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/steal",
        "http://wbsapi.withings.net/v2/x",
        "https://wbsapi.withings.net:8443/v2/x",
        "ftp://wbsapi.withings.net/x",
    ],
)
def test_every_redirect_hop_is_checked(target: str) -> None:
    start = f"{API}/v2/start"
    fake = FakeHTTPS(redirects={start: target})
    with pytest.raises(OutboundBlocked):
        _opener(fake).open(start, timeout=1)
    assert [u for u, _, _ in fake.seen] == [start]  # the hop never reached the network handler


def test_a_redirect_on_the_same_host_is_followed() -> None:
    start, target = f"{API}/v2/start", f"{API}/v2/next"
    fake = FakeHTTPS(redirects={start: target})
    with _opener(fake).open(start, timeout=1) as response:
        assert response.geturl() == target
    assert [u for u, _, _ in fake.seen] == [start, target]


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (f"{API}/v2/user", "https://account.withings.com/oauth2_user/authorize2"),
        ("https://account.withings.com/x", f"{API}/v2/user"),
        (f"{API}/v2/user", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1/x", f"{API}/v2/user"),
    ],
)
def test_a_redirect_to_another_listed_host_is_refused(start: str, target: str) -> None:
    """urllib copies the authorization header onto the next hop: never another host."""
    fake = FakeHTTPS(redirects={start: target})
    request = urllib.request.Request(start, headers={"authorization": "Bearer planted"})  # noqa: S310
    with pytest.raises(OutboundBlocked) as raised:
        _opener(fake).open(request, timeout=1)
    assert raised.value.host == urlsplit(target).hostname
    assert raised.value.after_request is True
    assert [u for u, _, _ in fake.seen] == [start]


@pytest.mark.parametrize("variable", PROXY_VARIABLES)
def test_a_proxy_in_the_environment_is_ignored(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, "http://proxy.evil.example:3128")
    monkeypatch.setenv("NO_PROXY", "")
    fake = FakeHTTPS()
    with _opener(fake).open(f"{API}/v2/x", timeout=1):
        pass
    assert fake.seen == [(f"{API}/v2/x", "wbsapi.withings.net", False)]


def test_a_request_with_a_proxy_set_on_it_is_refused() -> None:
    fake = FakeHTTPS()
    request = urllib.request.Request(f"{API}/v2/x")  # noqa: S310 - https, a fake handler
    request.set_proxy("proxy.evil.example:3128", "https")
    with pytest.raises(OutboundBlocked):
        _opener(fake).open(request, timeout=1)
    assert fake.seen == []


def test_http_errors_still_raise_http_error() -> None:
    with pytest.raises(urllib.error.HTTPError) as raised:
        _opener(FakeHTTPS(status=401)).open(f"{API}/v2/x", timeout=1)
    assert raised.value.code == 401


def test_the_opener_has_no_handler_for_other_schemes() -> None:
    opener = build_allowlist_opener()
    schemes = set(_field(opener, "handle_open")) - {"default", "unknown"}
    assert schemes == {"https"}
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in _field(opener, "handlers"))
    [https] = _field(opener, "handle_open")["https"]
    assert not isinstance(https, urllib.request.ProxyHandler)


def test_the_real_https_handler_checks_again_before_connecting() -> None:
    opener = build_allowlist_opener()
    [network] = [
        h for h in _field(opener, "handlers") if isinstance(h, urllib.request.HTTPSHandler)
    ]
    with pytest.raises(OutboundBlocked):
        network.https_open(urllib.request.Request("https://evil.example/"))  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize("ai_enabled", [False, True])
def test_install_makes_urlopen_use_the_worker_list(ai_enabled: bool) -> None:
    opener = install_worker_allowlist(ai_enabled=ai_enabled)
    assert _field(urllib.request, "_opener") is opener
    fake = FakeHTTPS()
    [network] = [
        h for h in _field(opener, "handlers") if isinstance(h, egress.DeadlineHTTPSHandler)
    ]
    network.https_open = fake.https_open  # pyright: ignore[reportAttributeAccessIssue]
    with urllib.request.urlopen(f"{API}/v2/user", timeout=1):  # noqa: S310
        pass
    with pytest.raises(OutboundBlocked):
        urllib.request.urlopen("https://evil.example/", timeout=1)
    if ai_enabled:
        with urllib.request.urlopen("https://api.anthropic.com/v1/x", timeout=1):
            pass
    else:
        with pytest.raises(OutboundBlocked):
            urllib.request.urlopen("https://api.anthropic.com/v1/x", timeout=1)
    assert len(fake.seen) == (2 if ai_enabled else 1)


def test_the_worker_list_is_withings_and_the_model_host_only_with_ai() -> None:
    withings = {"wbsapi.withings.net", "account.withings.com"}
    assert frozenset(withings) == egress.WORKER_HOSTS == egress.WITHINGS_HOSTS
    assert WORKER_ALLOWLIST.hosts == egress.WORKER_HOSTS
    assert egress.worker_allowlist().hosts == egress.WORKER_HOSTS
    assert egress.worker_hosts(ai_enabled=False) == frozenset(withings)
    assert egress.worker_hosts(ai_enabled=True) == frozenset({*withings, "api.anthropic.com"})


@pytest.mark.parametrize("value", ["false", "", 1, 0, None])
def test_ai_enabled_must_be_a_bool(value: object) -> None:
    with pytest.raises(TypeError):
        egress.worker_hosts(ai_enabled=value)  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        install_worker_allowlist(ai_enabled=value)  # pyright: ignore[reportArgumentType]
    assert not egress.worker_allowlist_installed()


@pytest.mark.parametrize("host", ["API.example", "", "trailing.dot."])
def test_an_allowlist_takes_only_lower_case_names(host: str) -> None:
    with pytest.raises(ValueError, match="lower case"):
        Allowlist(frozenset({host}))


# --- httpx -----------------------------------------------------------------------------------


class Recorder:
    def __init__(self, redirect_to: str | None = None) -> None:
        self.seen: list[str] = []
        self.redirect_to = redirect_to

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        if self.redirect_to is not None and len(self.seen) == 1:
            return httpx.Response(307, headers={"Location": self.redirect_to})
        return httpx.Response(200, json={"status": 0})


@pytest.mark.parametrize(
    "url", [f"{API}/v2/user", "https://account.withings.com/oauth2_user/authorize2"]
)
async def test_the_withings_client_reaches_both_withings_hosts(worker: None, url: str) -> None:
    recorder = Recorder()
    async with withings_async_client(inner=httpx.MockTransport(recorder)) as client:
        assert (await client.post(url, data={"action": "getdevice"})).status_code == 200
    assert recorder.seen == [url]


@pytest.mark.parametrize(
    ("url", "_host"),
    [*(b for b in BLOCKED if b[0].startswith("http")), ("https://api.anthropic.com/v1/x", "")],
)
async def test_the_withings_client_refuses_anything_else(
    worker: None, url: str, _host: str
) -> None:
    recorder = Recorder()
    async with withings_async_client(inner=httpx.MockTransport(recorder)) as client:
        with pytest.raises((OutboundBlocked, httpx.InvalidURL)):
            await client.get(url)
    assert recorder.seen == []


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/steal",
        "https://account.withings.com/oauth2_user/authorize2",
        "https://api.anthropic.com/v1/x",
        "//evil.example/protocol-relative",
    ],
)
@pytest.mark.parametrize("follow", [False, True])
async def test_the_withings_client_refuses_a_redirect_off_the_host(
    worker: None, target: str, follow: bool
) -> None:
    recorder = Recorder(redirect_to=target)
    async with withings_async_client(inner=httpx.MockTransport(recorder)) as client:
        with pytest.raises(OutboundBlocked) as raised:
            await client.get(
                f"{API}/v2/user",
                headers={"authorization": "Bearer planted"},
                follow_redirects=follow,
            )
    assert raised.value.after_request is True
    assert recorder.seen == [f"{API}/v2/user"]


async def test_the_withings_client_does_not_follow_a_same_host_redirect(worker: None) -> None:
    recorder = Recorder(redirect_to="http://wbsapi.withings.net/v2/other")
    async with withings_async_client(inner=httpx.MockTransport(recorder)) as client:
        assert client.follow_redirects is False
        response = await client.get(f"{API}/v2/user")
        assert response.status_code == 307  # handed back, not followed
    assert recorder.seen == [f"{API}/v2/user"]


@pytest.mark.parametrize("variable", PROXY_VARIABLES)
async def test_the_clients_ignore_proxy_settings(
    worker: None, variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, "http://proxy.evil.example:3128")
    for client in (withings_async_client(), model_async_client()):
        async with client:
            assert client.trust_env is False
            assert client._mounts == {}  # pyright: ignore[reportPrivateUsage]
            transport = client._transport  # pyright: ignore[reportPrivateUsage]
            assert isinstance(transport, egress.AllowlistTransport)


def test_no_client_takes_a_list(worker: None) -> None:
    for builder in (withings_async_client, model_async_client):
        with pytest.raises(TypeError):
            builder(WORKER_ALLOWLIST)  # pyright: ignore[reportCallIssue]
        with pytest.raises(TypeError):
            builder(allowlist=WORKER_ALLOWLIST)  # pyright: ignore[reportCallIssue]


async def test_the_model_client_reaches_the_model_host_alone(worker: None) -> None:
    """Its list is the model host, although the worker's list is wider."""
    recorder = Recorder()
    async with model_async_client(inner=httpx.MockTransport(recorder)) as client:
        await client.post("https://api.anthropic.com/v1/messages", json={})
        for url in (f"{API}/v2/user", "https://account.withings.com/x"):
            with pytest.raises(OutboundBlocked):
                await client.get(url)
    assert recorder.seen == ["https://api.anthropic.com/v1/messages"]


class Mapped(Exception):
    pass


async def test_refused_maps_a_refusal_with_the_refusal_as_its_cause(worker: None) -> None:
    client = model_async_client(inner=httpx.MockTransport(Recorder()), refused=lambda _: Mapped())
    async with client:
        with pytest.raises(Mapped) as raised:
            await client.get("https://evil.example/x")
    assert isinstance(raised.value.__cause__, OutboundBlocked)
    assert raised.value.__cause__.host == "evil.example"


async def test_the_model_client_has_the_adr_timeout_and_the_withings_client_a_total(
    worker: None,
) -> None:
    async with model_async_client() as client:
        assert client.timeout == httpx.Timeout(120.0)
    async with withings_async_client() as client:
        assert client.timeout == httpx.Timeout(egress.WITHINGS_TOTAL_SECONDS)
        transport = client._transport  # pyright: ignore[reportPrivateUsage]
        assert _field(transport, "_total_seconds") == egress.WITHINGS_TOTAL_SECONDS == 30.0


# --- TLS key log (SSLKEYLOGFILE) and trust store ----------------------------------------------


def _assert_no_key_log(context: ssl.SSLContext) -> None:
    assert context.keylog_filename is None
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.fixture
def key_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``SSLKEYLOGFILE`` set; the stdlib's default context would write it (checked here)."""
    path = tmp_path / "keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(path))
    assert ssl.create_default_context().keylog_filename == str(path)  # the risk is real
    path.unlink()
    return path


def test_the_tls_context_ignores_sslkeylogfile(key_log: Path) -> None:
    _assert_no_key_log(egress.tls_client_context())
    _assert_no_key_log(egress.tls_client_context(cafile=certifi.where()))
    assert not key_log.exists()


def test_the_urllib_opener_writes_no_tls_key_log(key_log: Path) -> None:
    opener = build_allowlist_opener(WORKER_ALLOWLIST)
    [https] = [h for h in _field(opener, "handlers") if isinstance(h, urllib.request.HTTPSHandler)]
    _assert_no_key_log(_field(https, "_context"))
    assert not key_log.exists()


def test_the_httpx_clients_write_no_tls_key_log(worker: None, key_log: Path) -> None:
    for client in (withings_async_client(), model_async_client()):  # built, never used
        transport = client._transport  # pyright: ignore[reportPrivateUsage]
        assert isinstance(transport, egress.AllowlistTransport)
        network = _field(transport, "_inner")
        assert isinstance(network, httpx.AsyncHTTPTransport)
        _assert_no_key_log(_field(_field(network, "_pool"), "_ssl_context"))
    assert not key_log.exists()


def test_the_network_transport_trusts_certifi_not_the_system_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str | None] = []
    real = core.tls_client_context

    def spy(*, cafile: str | None = None) -> ssl.SSLContext:
        asked.append(cafile)
        return real(cafile=cafile)

    monkeypatch.setattr(clients, "tls_client_context", spy)
    network = clients._network_transport()  # pyright: ignore[reportPrivateUsage]
    assert asked == [certifi.where()]  # switching to the system store (None) fails here
    context: ssl.SSLContext = _field(_field(network, "_pool"), "_ssl_context")
    expected = real(cafile=certifi.where()).get_ca_certs()
    assert expected  # the bundle is not empty
    assert context.get_ca_certs() == expected


def test_certifi_is_a_declared_dependency() -> None:
    """``ownvitals.egress`` imports it, so it must not rest on being pulled in by httpx."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text("utf-8"))["project"]["dependencies"]
    assert any(d.split(">")[0].split("=")[0].split("[")[0].strip() == "certifi" for d in declared)
