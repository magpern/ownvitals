"""The web process's one Withings call (roadmap Amendment 1, ``egress.code_exchange``): the caller
hands in four values, the package builds ``POST https://wbsapi.withings.net/v2/oauth2`` with
``action=requesttoken`` and ``grant_type=authorization_code``, and the transport checks the final
bytes again before the network transport sees anything. Unusable from the worker. Synthetic
values and a recording fake transport only."""

import gzip
import logging
import urllib.request
from collections.abc import AsyncIterator
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest

from ownvitals.egress import (
    CODE_EXCHANGE_FIELDS,
    CODE_EXCHANGE_HEADERS,
    CODE_EXCHANGE_TOTAL_SECONDS,
    WITHINGS_TOKEN_URL,
    OutboundBlocked,
    code_exchange,
    core,
    encode_code_exchange,
    exchange_withings_code,
    install_worker_allowlist,
    is_code_exchange_form,
)

VALUES = {
    "client_id": "synthetic-client-id",
    "client_secret": "planted-client-secret",
    "code": "planted-authorization-code",
    "redirect_uri": "https://ownvitals.example/api/oauth/withings/callback",
}
FORM = {"action": "requesttoken", "grant_type": "authorization_code", **VALUES}
FORM_TYPE = "application/x-www-form-urlencoded"


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


class Recorder:
    def __init__(self, answer: httpx.Response | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self.answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.answer is not None:
            return self.answer
        return httpx.Response(200, json={"status": 0, "body": {"userid": 1}})  # synthetic


async def _exchange(recorder: Recorder, **values: Any) -> httpx.Response:
    return await exchange_withings_code(**{**VALUES, **values}, inner=httpx.MockTransport(recorder))


async def test_the_exchange_goes_out_as_built_here() -> None:
    recorder = Recorder()
    response = await _exchange(recorder)
    assert response.json()["status"] == 0
    [request] = recorder.seen
    assert (request.method, str(request.url)) == ("POST", "https://wbsapi.withings.net/v2/oauth2")
    assert parse_qsl(request.content.decode("ascii")) == list(FORM.items())
    assert request.content == urlencode(list(FORM.items())).encode("ascii")
    assert {name.lower() for name, _ in request.headers.multi_items()} == CODE_EXCHANGE_HEADERS
    assert request.headers["content-type"] == FORM_TYPE
    assert request.headers["accept"] == "application/json"


def test_the_body_parses_back_to_exactly_the_fields() -> None:
    body = encode_code_exchange(**VALUES)
    assert [name for name, _ in parse_qsl(body.decode("ascii"))] == list(CODE_EXCHANGE_FIELDS)
    assert dict(parse_qsl(body.decode("ascii"))) == FORM
    assert is_code_exchange_form(body)


BAD_VALUES = [
    "a;grant_type=refresh_token",
    "a;b",
    "a&grant_type=refresh_token",
    "a&b",
    "a=b",
    "a#b",
    "a\rb",
    "a\nb",
    "a\r\nX-Injected: 1",
    "a\x00b",
    "a\tb",
    "a\x7fb",
    "a b",
    "a" + chr(0x2028) + "b",  # a Unicode line separator
    "aé",
    "",
]


@pytest.mark.parametrize("field", sorted(VALUES))
@pytest.mark.parametrize("value", BAD_VALUES)
async def test_a_value_with_a_separator_or_control_character_is_refused_before_encoding(
    field: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded: list[object] = []
    monkeypatch.setattr(code_exchange, "urlencode", lambda *a, **k: encoded.append(a) or "")  # pyright: ignore[reportUnknownLambdaType]
    with pytest.raises(OutboundBlocked):
        encode_code_exchange(**{**VALUES, field: value})
    assert encoded == []
    monkeypatch.undo()
    recorder = Recorder()
    with pytest.raises(OutboundBlocked):
        await _exchange(recorder, **{field: value})
    assert recorder.seen == []


async def test_a_value_that_is_not_a_string_is_refused() -> None:
    recorder = Recorder()
    for value in (None, 1, b"code", ["code"]):
        with pytest.raises(OutboundBlocked):
            await _exchange(recorder, code=cast(Any, value))
    assert recorder.seen == []


async def test_a_redirect_answer_is_refused_and_never_followed() -> None:
    """The form holds the code and the client secret: no hop gets it, not even the same URL."""
    for location in (WITHINGS_TOKEN_URL, "/v2/oauth2", "https://evil.example/steal"):
        recorder = Recorder(httpx.Response(307, headers={"Location": location}))
        with pytest.raises(OutboundBlocked) as raised:
            await _exchange(recorder)
        assert raised.value.after_request is True
        assert len(recorder.seen) == 1


async def test_a_refusal_logs_the_host_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(OutboundBlocked) as raised:
        await _exchange(Recorder(), code="planted-authorization-code;action=revoke")
    assert [r.getMessage() for r in caplog.records] == ["outbound request blocked"]
    shown = caplog.text + repr(raised.value) + repr(raised.value.args)
    for secret in ("planted-client-secret", "planted-authorization-code", "revoke"):
        assert secret not in shown
    assert raised.value.host == "wbsapi.withings.net"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_the_exchange_is_refused_in_the_worker() -> None:
    recorder = Recorder()
    transport = code_exchange._code_exchange_transport(10.0, httpx.MockTransport(recorder))  # pyright: ignore[reportPrivateUsage]
    install_worker_allowlist()
    with pytest.raises(OutboundBlocked):  # not built at all
        await _exchange(recorder)
    async with httpx.AsyncClient(transport=transport) as client:  # built before: per request
        with pytest.raises(OutboundBlocked):
            await client.send(_request(encode_code_exchange(**VALUES)))
    assert recorder.seen == []


async def test_the_client_has_its_total_deadline_and_no_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[httpx.AsyncClient] = []

    class Recording(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            built.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", Recording)
    await _exchange(Recorder())
    [client] = built
    assert client.trust_env is False
    assert client.follow_redirects is False
    assert client._mounts == {}  # pyright: ignore[reportPrivateUsage]
    assert client.timeout == httpx.Timeout(CODE_EXCHANGE_TOTAL_SECONDS)
    transport = client._transport  # pyright: ignore[reportPrivateUsage]
    assert isinstance(transport, code_exchange._CodeExchangeTransport)  # pyright: ignore[reportPrivateUsage]
    assert getattr(transport, "_total_seconds") == 10.0  # noqa: B009
    assert getattr(transport, "_allowlist").hosts == {"wbsapi.withings.net"}  # noqa: B009
    assert getattr(transport, "_role") == "web"  # noqa: B009


# The transport's own checks, whatever reaches it: a request built by hand, past the encoder.
def _request(
    body: bytes | None = None,
    *,
    method: str = "POST",
    url: str = WITHINGS_TOKEN_URL,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    **kwargs: Any,
) -> httpx.Request:
    if headers is None:
        headers = {"Content-Type": FORM_TYPE}
    return httpx.Request(method, url, content=body, headers=headers, **kwargs)


async def _send_raw(request: httpx.Request) -> list[httpx.Request]:
    recorder = Recorder()
    transport = code_exchange._code_exchange_transport(10.0, httpx.MockTransport(recorder))  # pyright: ignore[reportPrivateUsage]
    async with httpx.AsyncClient(transport=transport) as client:
        await client.send(request)
    return recorder.seen


def _raw(pairs: list[tuple[str, str]]) -> bytes:
    return "&".join(f"{k}={v}" for k, v in pairs).encode()  # as given, unencoded


GOOD = urlencode(list(FORM.items())).encode("ascii")


async def test_the_transport_admits_the_exchange() -> None:
    assert len(await _send_raw(_request(GOOD))) == 1
    minimal = urlencode({"action": "requesttoken", "grant_type": "authorization_code", "code": "c"})
    assert len(await _send_raw(_request(minimal.encode()))) == 1


REFUSED_BODIES: list[bytes] = [
    _raw([*{**FORM, "grant_type": "refresh_token"}.items()]),
    _raw([*{**FORM, "grant_type": "client_credentials"}.items()]),
    _raw([*{**FORM, "grant_type": ""}.items()]),
    _raw([*{**FORM, "action": "revoke"}.items()]),
    _raw([*{**FORM, "action": "getnonce"}.items()]),
    _raw([*{**FORM, "action": "REQUESTTOKEN"}.items()]),
    _raw([*{**FORM, "code": ""}.items()]),
    _raw([(k, v) for k, v in FORM.items() if k != "action"]),
    _raw([(k, v) for k, v in FORM.items() if k != "grant_type"]),
    _raw([(k, v) for k, v in FORM.items() if k != "code"]),
    _raw([*FORM.items(), ("refresh_token", "planted-refresh-token")]),
    _raw([*FORM.items(), ("userid", "1")]),
    _raw([*FORM.items(), ("grant.type", "refresh_token")]),  # PHP folds "." into "_"
    _raw([*FORM.items(), ("grant_type[]", "refresh_token")]),
    _raw([*FORM.items(), ("grant_type", "refresh_token")]),  # the field twice
    _raw([*FORM.items(), ("gr%61nt_type", "refresh_token")]),  # the same, percent-encoded
    _raw([*FORM.items(), ("action", "revoke")]),
    # A second field hidden in a value, raw or percent-encoded, and other non-canonical forms.
    GOOD.replace(b"code=planted", b"code=planted;grant_type=refresh_token;x"),
    GOOD.replace(b"code=planted", b"code=planted%3Bgrant_type%3Drefresh_token"),
    GOOD.replace(b"code=planted", b"code=planted%26grant_type%3Drefresh_token"),
    GOOD.replace(b"code=planted", b"code=planted%23"),
    GOOD.replace(b"code=planted", b"code=planted%0D%0Ax"),
    GOOD.replace(b"code=planted", b"code=planted%0Ax"),
    GOOD.replace(b"code=planted", b"code=planted%C3%A9"),
    GOOD.replace(b"code=planted", b"code=planted+x"),
    GOOD.replace(b"code=planted", b"code=planted%20x"),
    GOOD.replace(b"code=planted", b"code=pl%61nted"),  # an unreserved character, encoded
    GOOD.replace(b"code=planted", b"code=planted#x"),
    GOOD + b"&",
    GOOD + b"\r\n",
    "action=requesttoken&grant_type=authorization_code&code=é".encode(),
    urlencode({**FORM, "code": "c" * 5000}).encode(),  # over 4 KiB
    b"",
]


@pytest.mark.parametrize("body", REFUSED_BODIES)
async def test_the_transport_refuses_any_other_form(body: bytes) -> None:
    with pytest.raises(OutboundBlocked) as raised:
        await _send_raw(_request(body))
    assert raised.value.host == "wbsapi.withings.net"


REFUSED_HEADERS: list[list[tuple[str, str]]] = [
    [("Content-Type", FORM_TYPE), ("Authorization", "Bearer planted")],
    [("Content-Type", FORM_TYPE), ("Cookie", "session=planted")],
    [("Content-Type", FORM_TYPE), ("Accept-Encoding", "gzip")],
    [("Content-Type", FORM_TYPE), ("X-Forwarded-For", "203.0.113.1")],
    [("Content-Type", FORM_TYPE), ("Connection", "keep-alive")],
    [("Content-Type", FORM_TYPE), ("Content-Type", FORM_TYPE)],
    [("Content-Type", FORM_TYPE), ("Host", "evil.example")],
    [("Content-Type", FORM_TYPE), ("Content-Length", "5")],
    [("Content-Type", FORM_TYPE), ("Content-Encoding", "identity")],
    [("Content-Type", "application/json")],
    [("Content-Type", FORM_TYPE + "; charset=utf-8")],
    [],
]


@pytest.mark.parametrize("headers", REFUSED_HEADERS)
async def test_the_transport_refuses_any_other_header(headers: list[tuple[str, str]]) -> None:
    with pytest.raises(OutboundBlocked):
        await _send_raw(_request(GOOD, headers=headers))


async def test_a_streamed_body_without_its_length_is_refused() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        yield GOOD

    with pytest.raises(OutboundBlocked):
        await _send_raw(
            httpx.Request(
                "POST", WITHINGS_TOKEN_URL, content=chunks(), headers={"Content-Type": FORM_TYPE}
            )
        )


async def test_the_client_defaults_of_httpx_are_refused() -> None:
    """``client.post`` adds httpx's default headers (``Accept-Encoding``, ``Connection`` ...):
    only the request built in the package goes out."""
    recorder = Recorder()
    transport = code_exchange._code_exchange_transport(10.0, httpx.MockTransport(recorder))  # pyright: ignore[reportPrivateUsage]
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(OutboundBlocked):
            await client.post(WITHINGS_TOKEN_URL, content=GOOD, headers={"Content-Type": FORM_TYPE})
        with pytest.raises(OutboundBlocked):
            await client.post(WITHINGS_TOKEN_URL, data=FORM)
    assert recorder.seen == []


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", WITHINGS_TOKEN_URL),
        ("PUT", WITHINGS_TOKEN_URL),
        ("POST", "https://wbsapi.withings.net/v2/oauth2/"),
        ("POST", "https://wbsapi.withings.net/v2/%6Fauth2"),
        ("POST", "https://wbsapi.withings.net/V2/oauth2"),
        ("POST", "https://wbsapi.withings.net/v2/oauth2?action=revoke"),
        ("POST", "https://wbsapi.withings.net/v2/user"),
        ("POST", "https://wbsapi.withings.net/measure"),
        ("POST", "https://account.withings.com/oauth2/token"),
        ("POST", "https://account.withings.com/v2/oauth2"),
        ("POST", "http://wbsapi.withings.net/v2/oauth2"),
        ("POST", "https://wbsapi.withings.net:8443/v2/oauth2"),
        ("POST", "https://user:pw@wbsapi.withings.net/v2/oauth2"),
        ("POST", "https://wbsapi.withings.net.evil.example/v2/oauth2"),
        ("POST", "https://oauth2.googleapis.com/token"),
        ("POST", "https://api.anthropic.com/v1/messages"),
    ],
)
async def test_the_transport_refuses_any_other_method_host_or_path(method: str, url: str) -> None:
    host = httpx.URL(url).host
    headers = [("Content-Type", FORM_TYPE), ("Host", host)] if host else None
    with pytest.raises(OutboundBlocked):
        await _send_raw(_request(GOOD, method=method, url=url, headers=headers))


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        (b'{"action": "requesttoken"}', [("Content-Type", "application/json")]),
        (gzip.compress(GOOD), [("Content-Type", FORM_TYPE), ("Content-Encoding", "gzip")]),
    ],
)
async def test_the_transport_refuses_any_other_encoding(
    body: bytes, headers: list[tuple[str, str]]
) -> None:
    with pytest.raises(OutboundBlocked):
        await _send_raw(_request(body, headers=headers))


def test_the_form_check() -> None:
    assert CODE_EXCHANGE_FIELDS == (
        "action",
        "grant_type",
        "client_id",
        "client_secret",
        "code",
        "redirect_uri",
    )
    assert is_code_exchange_form(GOOD)
    for body in (b"", b"&", b"=", b"action", b"\xff", b"code=c&" * 10, *REFUSED_BODIES):
        assert not is_code_exchange_form(body)
