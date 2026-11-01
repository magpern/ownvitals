"""The process roles of the outbound allowlist (PLAN §2, roadmap Amendment 1), at run time.

What each process can reach, enumerated: every entry point of ``ownvitals.egress`` and the one
exempt site (mail) against every candidate request, in the web process and in the worker with AI
off and on. Web reaches Google's sign-in hosts, the one Withings authorization-code exchange and
the configured SMTP server; the worker reaches Withings, and the model host only with AI. And no
test reaches Withings over the network: ``netguard`` stops the real network transports."""

import contextlib
import inspect
import io
import sys
import urllib.request
import urllib.response
from collections.abc import Awaitable, Callable
from email.message import Message
from typing import Any, ClassVar

import httpx
import pytest

import netguard
from ownvitals import egress
from ownvitals.config import Settings
from ownvitals.egress import (
    OutboundBlocked,
    core,
    exchange_withings_code,
    google_async_client,
    google_oauth_client,
    install_worker_allowlist,
    model_async_client,
    withings_async_client,
)
from ownvitals.mail import Mailer, MailMessage
from smtpfake import SmtpFake

API = "https://wbsapi.withings.net"
VALUES: dict[str, Any] = {
    "client_id": "synthetic-client",
    "client_secret": "synthetic-secret",
    "code": "synthetic-code",
    "redirect_uri": "https://ownvitals.example/api/oauth/withings/callback",
}
EXCHANGE = {"action": "requesttoken", "grant_type": "authorization_code", **VALUES}
GOOGLE_VALUES: dict[str, Any] = {
    "client_id": "synthetic-client",
    "client_secret": "synthetic-secret",
    "scope": "openid email",
    "redirect_uri": "https://ownvitals.example/api/oauth/google/callback",
}
MESSAGE = MailMessage(to="someone@example.com", subject="s", body="b")


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


def test_the_worker_flag_is_set_only_by_installing_the_allowlist() -> None:
    assert not egress.worker_allowlist_installed()
    assert egress.installed_worker_hosts() == frozenset()
    egress.refuse_in_worker("anything.example")  # no-op outside the worker
    install_worker_allowlist()
    assert egress.worker_allowlist_installed()
    assert egress.installed_worker_hosts() == egress.WITHINGS_HOSTS
    with pytest.raises(OutboundBlocked):
        egress.refuse_in_worker("anything.example")


# --- web-only sites in the worker, worker-only clients in web --------------------------------


WEB_ONLY: list[tuple[Callable[..., Any], str]] = [
    (google_async_client, "oauth2.googleapis.com"),
    (lambda: google_oauth_client(**GOOGLE_VALUES), "oauth2.googleapis.com"),
    (lambda: exchange_withings_code(**VALUES), "wbsapi.withings.net"),
]
WORKER_ONLY: list[tuple[Callable[..., Any], str]] = [
    (withings_async_client, "wbsapi.withings.net"),
    (model_async_client, "api.anthropic.com"),
]


@pytest.mark.parametrize(("builder", "host"), WEB_ONLY)
async def test_a_web_only_entry_point_is_refused_in_the_worker(
    builder: Callable[..., Any], host: str
) -> None:
    install_worker_allowlist(ai_enabled=True)

    async def call() -> None:
        result = builder()
        if inspect.isawaitable(result):
            await result

    with pytest.raises(OutboundBlocked) as raised:
        await call()
    assert raised.value.host == host


@pytest.mark.parametrize(("builder", "host"), WORKER_ONLY)
def test_a_worker_only_client_is_refused_in_web(builder: Callable[..., Any], host: str) -> None:
    with pytest.raises(OutboundBlocked) as raised:
        builder()
    assert raised.value.host == host


async def test_the_model_client_is_refused_in_a_worker_without_ai() -> None:
    install_worker_allowlist(ai_enabled=False)
    with pytest.raises(OutboundBlocked) as raised:
        model_async_client()
    assert raised.value.host == "api.anthropic.com"
    install_worker_allowlist(ai_enabled=True)  # and with AI it builds
    async with model_async_client() as client:
        assert isinstance(client, httpx.AsyncClient)


class Recorder:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        return httpx.Response(200, json={"status": 0})


# --- Google sign-in through the package's OAuth helper (ADR 0003 §5, #10) -------------------


async def test_the_google_oauth_client_is_authlibs_over_the_signin_transport() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        token = {"access_token": "synthetic", "token_type": "Bearer", "expires_in": 3600}
        return httpx.Response(200, json=token)

    seen: list[httpx.Request] = []
    async with google_oauth_client(**GOOGLE_VALUES, inner=httpx.MockTransport(answer)) as client:
        assert type(client).__name__ == "AsyncOAuth2Client"
        assert (client.trust_env, client.follow_redirects) == (False, False)
        assert client._mounts == {}  # pyright: ignore[reportPrivateUsage]
        assert client.timeout == httpx.Timeout(egress.GOOGLE_TOTAL_SECONDS)
        assert client.code_challenge_method == "S256"
        assert client.token_endpoint_auth_method == "client_secret_post"
        transport = client._transport  # pyright: ignore[reportPrivateUsage]
        assert isinstance(transport, egress.AllowlistTransport)
        assert getattr(transport, "_allowlist") is egress.GOOGLE_SIGNIN_ALLOWLIST  # noqa: B009
        assert getattr(transport, "_role") == "web"  # noqa: B009
        assert getattr(transport, "_total_seconds") == egress.GOOGLE_TOTAL_SECONDS  # noqa: B009
        token = await client.fetch_token(
            "https://oauth2.googleapis.com/token", code="synthetic-code", code_verifier="v" * 43
        )
        assert token["access_token"] == "synthetic"
        with pytest.raises(OutboundBlocked):
            await client.fetch_token("https://evil.example/token", code="synthetic-code")
    assert [r.url.host for r in seen] == ["oauth2.googleapis.com"]


async def test_the_google_oauth_client_refuses_a_redirect_to_another_host() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/token"})

    async with google_oauth_client(**GOOGLE_VALUES, inner=httpx.MockTransport(answer)) as client:
        with pytest.raises(OutboundBlocked):
            await client.fetch_token("https://oauth2.googleapis.com/token", code="synthetic-code")


def test_the_google_oauth_client_must_run_on_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authlib runs on httpx2 when that is installed: then its client could not take the
    package's transport, and the helper refuses to build it."""
    google_oauth_client(**GOOGLE_VALUES)  # imports Authlib's httpx integration
    authlib_httpx = sys.modules["authlib.integrations.httpx_client"]

    class OnAnotherLibrary:
        def __init__(self, **_: Any) -> None:
            raise AssertionError("never built")

    monkeypatch.setattr(authlib_httpx, "AsyncOAuth2Client", OnAnotherLibrary)
    with pytest.raises(TypeError, match="must run on httpx"):
        google_oauth_client(**GOOGLE_VALUES)


async def test_each_request_checks_the_role_again() -> None:
    """Built in the right process, a client still refuses a request from the wrong one: the
    check runs per request, not only when the client is built."""
    recorder = Recorder()
    web = google_async_client(inner=httpx.MockTransport(recorder))
    install_worker_allowlist()
    worker = withings_async_client(inner=httpx.MockTransport(recorder))
    async with web, worker:
        with pytest.raises(OutboundBlocked):
            await web.get("https://www.googleapis.com/oauth2/v3/certs")
        core._worker_list = None  # pyright: ignore[reportPrivateUsage] - back to "web"
        with pytest.raises(OutboundBlocked):
            await worker.get(f"{API}/v2/user")
    assert recorder.seen == []


class NoSmtp:
    """Stands in for the mailer's SMTP client: records a connection attempt, never connects."""

    calls: ClassVar[list[str]] = []

    def __init__(self, host: str, *_: Any, **__: Any) -> None:
        NoSmtp.calls.append(host)
        raise ConnectionRefusedError("test: no SMTP")


@pytest.fixture
def no_smtp(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    NoSmtp.calls = []
    monkeypatch.setattr("ownvitals.mail._DeadlineSMTP", NoSmtp)
    return NoSmtp.calls


def _mailer() -> Mailer:
    return Mailer(Settings(smtp_host="mailpit", smtp_port=1025, smtp_tls="none"))


async def test_mail_is_refused_in_the_worker_before_any_connection(no_smtp: list[str]) -> None:
    install_worker_allowlist()
    with pytest.raises(OutboundBlocked) as raised:
        await _mailer().send(MESSAGE)
    assert raised.value.host == "mailpit"
    with pytest.raises(OutboundBlocked):
        _mailer()._send_blocking(MESSAGE)  # pyright: ignore[reportPrivateUsage]
    assert no_smtp == []


async def test_mutation_without_the_worker_flag_mail_would_connect(no_smtp: list[str]) -> None:
    """The same call outside the worker reaches the SMTP client: the refusal above is the flag."""
    with pytest.raises(Exception, match="delivery failed"):
        await _mailer().send(MESSAGE)
    assert no_smtp == ["mailpit"]


# --- what each process can reach --------------------------------------------------------------

# (method, url, form): every host either process could want, and the Withings token calls.
CANDIDATES: list[tuple[str, str, dict[str, str] | None]] = [
    ("POST", f"{API}/measure", {"action": "getmeas"}),
    ("POST", f"{API}/v2/oauth2", EXCHANGE),
    ("POST", f"{API}/v2/oauth2", {**EXCHANGE, "grant_type": "refresh_token"}),
    ("POST", f"{API}/v2/oauth2", {"action": "revoke", "client_id": "c", "userid": "1"}),
    ("GET", "https://account.withings.com/oauth2_user/authorize2", None),
    ("POST", "https://api.anthropic.com/v1/messages", None),
    ("POST", "https://oauth2.googleapis.com/token", {"grant_type": "authorization_code"}),
    ("GET", "https://www.googleapis.com/oauth2/v3/certs", None),
    ("GET", "https://accounts.google.com/.well-known/openid-configuration", None),
    ("GET", "https://evil.example/", None),
]
HTTPX_ENTRY_POINTS: dict[str, Callable[[httpx.AsyncBaseTransport], httpx.AsyncClient]] = {
    "withings_async_client": lambda inner: withings_async_client(inner=inner),
    "model_async_client": lambda inner: model_async_client(inner=inner),
    "google_async_client": lambda inner: google_async_client(inner=inner),
    "google_oauth_client": lambda inner: google_oauth_client(**GOOGLE_VALUES, inner=inner),
}
# Authlib's client sends no request without a token unless told to withhold it.
REQUEST_OPTIONS: dict[str, dict[str, Any]] = {"google_oauth_client": {"withhold_token": True}}
# Entry points that send one request they build themselves: called once, as a caller would.
FIXED_REQUESTS: dict[str, Callable[[httpx.AsyncBaseTransport], Awaitable[httpx.Response]]] = {
    "exchange_withings_code": lambda inner: exchange_withings_code(**VALUES, inner=inner),
}


def _what(request: httpx.Request) -> str:
    """``host`` and, for the Withings token endpoint, the action and grant type it carried."""
    what = request.url.host
    if request.url.path == "/v2/oauth2":
        form = dict(httpx.QueryParams(request.content.decode()))
        what += f" {form.get('action')} {form.get('grant_type', '-')}"
    return what


async def _reach_over_httpx() -> set[str]:
    reached: set[str] = set()

    def record(request: httpx.Request) -> httpx.Response:
        reached.add(_what(request))
        return httpx.Response(200, json={"status": 0})

    for name, build in HTTPX_ENTRY_POINTS.items():
        try:
            client = build(httpx.MockTransport(record))
        except OutboundBlocked:
            continue
        async with client:
            for method, url, form in CANDIDATES:
                with contextlib.suppress(OutboundBlocked):
                    await client.request(method, url, data=form, **REQUEST_OPTIONS.get(name, {}))
    for send in FIXED_REQUESTS.values():
        with contextlib.suppress(OutboundBlocked):
            await send(httpx.MockTransport(record))
    return reached


class FakeHTTPS(urllib.request.BaseHandler):
    def __init__(self, reached: set[str]) -> None:
        self.reached = reached

    def https_open(self, req: urllib.request.Request) -> Any:
        self.reached.add(req.host)
        response = urllib.response.addinfourl(io.BytesIO(b"{}"), Message(), req.full_url, 200)
        response.msg = "fake"  # pyright: ignore[reportAttributeAccessIssue]
        return response


def _reach_over_urllib(opener: urllib.request.OpenerDirector | None) -> set[str]:
    reached: set[str] = set()
    if opener is None:
        return reached  # the web process installs no opener: its urllib is the guard's to keep out
    handlers: list[Any] = opener.handlers  # pyright: ignore[reportAttributeAccessIssue]
    [network] = [h for h in handlers if isinstance(h, egress.DeadlineHTTPSHandler)]
    network.https_open = FakeHTTPS(reached).https_open  # pyright: ignore[reportAttributeAccessIssue]
    for _, url, _ in CANDIDATES:
        with contextlib.suppress(OutboundBlocked):
            urllib.request.urlopen(url, timeout=1).close()  # noqa: S310
    return reached


async def _reach_over_smtp() -> set[str]:
    async with SmtpFake() as smtp:
        mailer = Mailer(Settings(smtp_host="127.0.0.1", smtp_port=smtp.port, smtp_tls="none"))
        with contextlib.suppress(OutboundBlocked):
            await mailer.send(MESSAGE)
    return {"smtp (the configured host)"} if smtp.messages else set()


async def _reach(process: Callable[[], urllib.request.OpenerDirector | None]) -> set[str]:
    opener = process()
    return await _reach_over_httpx() | _reach_over_urllib(opener) | await _reach_over_smtp()


GOOGLE = {"oauth2.googleapis.com", "www.googleapis.com"}
WITHINGS_WORKER = {
    "wbsapi.withings.net",
    "wbsapi.withings.net requesttoken authorization_code",
    "wbsapi.withings.net requesttoken refresh_token",
    "wbsapi.withings.net revoke -",
    "account.withings.com",
}
PROCESSES: dict[str, tuple[Callable[[], urllib.request.OpenerDirector | None], set[str]]] = {
    "web": (
        lambda: None,
        {
            *GOOGLE,
            "wbsapi.withings.net requesttoken authorization_code",
            "smtp (the configured host)",
        },
    ),
    "worker": (lambda: install_worker_allowlist(ai_enabled=False), WITHINGS_WORKER),
    "worker with AI": (
        lambda: install_worker_allowlist(ai_enabled=True),
        {*WITHINGS_WORKER, "api.anthropic.com"},
    ),
}


@pytest.mark.parametrize("name", PROCESSES)
async def test_what_each_process_can_reach(name: str) -> None:
    process, expected = PROCESSES[name]
    assert await _reach(process) == expected


def test_web_reaches_withings_only_through_the_code_exchange() -> None:
    """Roadmap Amendment 1: no refresh, revoke or other Withings request from web."""
    web = PROCESSES["web"][1]
    assert {w for w in web if w.startswith("wbsapi") or w.startswith("account")} == {
        "wbsapi.withings.net requesttoken authorization_code"
    }


def _returns(function: Callable[..., Any]) -> str:
    """The name of what ``function`` returns, also when it is annotated as a string."""
    annotation = inspect.signature(function).return_annotation
    return annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")


def test_the_enumeration_covers_every_client_the_package_builds() -> None:
    """A new client builder or request sender in ``ownvitals.egress`` must join the enumeration
    above (by what it returns: a client, a transport, or an answer)."""
    built = {"AsyncClient", "GoogleOAuthClient", "AllowlistTransport", "Response"}
    builders = {
        name
        for name, value in vars(egress).items()
        if inspect.isfunction(value) and not name.startswith("_") and _returns(value) in built
    }
    assert builders == set(HTTPX_ENTRY_POINTS) | set(FIXED_REQUESTS)


# --- no Withings network access in tests (netguard) -------------------------------------------


async def _raises_network_blocked(call: Callable[[], Awaitable[Any]]) -> None:
    with pytest.raises(netguard.NetworkBlocked):
        await call()


async def test_the_real_withings_transport_is_stopped_by_the_netguard() -> None:
    """Without ``inner`` the clients use the real network transport: in a test, ``netguard``
    refuses the lookup before a packet leaves, whatever the allowlist let through."""
    await _raises_network_blocked(lambda: exchange_withings_code(**VALUES))
    async with google_oauth_client(**GOOGLE_VALUES) as google:
        await _raises_network_blocked(
            lambda: google.request(
                "POST", "https://oauth2.googleapis.com/token", withhold_token=True
            )
        )
    install_worker_allowlist()
    async with withings_async_client() as client:
        await _raises_network_blocked(lambda: client.post(f"{API}/measure", data={"a": "b"}))


def test_the_real_urllib_handler_is_stopped_by_the_netguard() -> None:
    install_worker_allowlist()
    with pytest.raises(netguard.NetworkBlocked):
        urllib.request.urlopen(f"{API}/v2/user", timeout=5)  # noqa: S310
