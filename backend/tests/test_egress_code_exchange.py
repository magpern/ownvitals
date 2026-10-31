"""The web process's one Withings entry point (roadmap Amendment 1, ``egress.code_exchange``): it
admits ``POST https://wbsapi.withings.net/v2/oauth2`` with ``action=requesttoken`` and
``grant_type=authorization_code`` and nothing else, before the network transport sees anything,
and it is unusable from the worker. Synthetic values and a recording fake transport only."""

import gzip
import logging
import urllib.request
from typing import Any

import httpx
import pytest

from ownvitals import egress
from ownvitals.egress import (
    CODE_EXCHANGE_FIELDS,
    WITHINGS_TOKEN_URL,
    OutboundBlocked,
    core,
    install_worker_allowlist,
    is_code_exchange_form,
    withings_code_exchange_client,
)

FORM = {
    "action": "requesttoken",
    "grant_type": "authorization_code",
    "client_id": "synthetic-client-id",
    "client_secret": "planted-client-secret",
    "code": "planted-authorization-code",
    "redirect_uri": "https://ownvitals.example/api/oauth/withings/callback",
}


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


class Recorder:
    def __init__(self, answer: httpx.Response | None = None) -> None:
        self.seen: list[tuple[str, str, bytes]] = []
        self.answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, str(request.url), request.content))
        if self.answer is not None:
            return self.answer
        return httpx.Response(200, json={"status": 0, "body": {"userid": 1}})  # synthetic


async def _send(recorder: Recorder, method: str, url: str, **kwargs: Any) -> httpx.Response:
    async with withings_code_exchange_client(inner=httpx.MockTransport(recorder)) as client:
        return await client.request(method, url, **kwargs)


async def test_the_exchange_goes_out() -> None:
    recorder = Recorder()
    response = await _send(recorder, "POST", WITHINGS_TOKEN_URL, data=FORM)
    assert response.json()["status"] == 0
    [(method, url, body)] = recorder.seen
    assert (method, url) == ("POST", "https://wbsapi.withings.net/v2/oauth2")
    assert dict(httpx.QueryParams(body.decode())) == FORM


async def test_a_minimal_exchange_goes_out_too() -> None:
    recorder = Recorder()
    form = {"action": "requesttoken", "grant_type": "authorization_code", "code": "c"}
    await _send(recorder, "POST", WITHINGS_TOKEN_URL, data=form)
    assert len(recorder.seen) == 1


REFUSED_FORMS: list[dict[str, str] | list[tuple[str, str]]] = [
    {**FORM, "grant_type": "refresh_token"},
    {**FORM, "grant_type": "client_credentials"},
    {**FORM, "grant_type": ""},
    {**FORM, "action": "revoke"},
    {**FORM, "action": "getnonce"},
    {**FORM, "action": "requesttoken "},
    {**FORM, "action": "REQUESTTOKEN"},
    {**FORM, "grant_type": "Authorization_Code"},
    {**FORM, "code": ""},
    {k: v for k, v in FORM.items() if k != "action"},
    {k: v for k, v in FORM.items() if k != "grant_type"},
    {k: v for k, v in FORM.items() if k != "code"},
    {**FORM, "refresh_token": "planted-refresh-token"},
    {**FORM, "userid": "1"},
    {**FORM, "grant.type": "refresh_token"},  # a PHP-style parser folds "." into "_"
    {**FORM, "grant_type[]": "refresh_token"},
    [*FORM.items(), ("grant_type", "refresh_token")],  # the field twice
    [*FORM.items(), ("gr%61nt_type", "refresh_token")],  # the same, percent-encoded
    [*FORM.items(), ("action", "revoke")],
]


@pytest.mark.parametrize("form", REFUSED_FORMS)
async def test_any_other_form_is_refused_before_it_is_sent(
    form: dict[str, str] | list[tuple[str, str]],
) -> None:
    recorder = Recorder()
    if isinstance(form, list):
        body = "&".join(f"{k}={v}" for k, v in form).encode()  # keys as given, unencoded
        kwargs: dict[str, Any] = {
            "content": body,
            "headers": {"content-type": "application/x-www-form-urlencoded"},
        }
    else:
        kwargs = {"data": form}
    with pytest.raises(OutboundBlocked) as raised:
        await _send(recorder, "POST", WITHINGS_TOKEN_URL, **kwargs)
    assert raised.value.host == "wbsapi.withings.net"
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
        ("POST", "https://wbsapi.withings.net/oauth2/token"),
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
async def test_any_other_method_host_or_path_is_refused(method: str, url: str) -> None:
    recorder = Recorder()
    with pytest.raises(OutboundBlocked):
        await _send(recorder, method, url, data=FORM if method != "GET" else None)
    assert recorder.seen == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": FORM},  # application/json
        {"files": {"code": b"c"}, "data": FORM},  # multipart
        {
            "content": gzip.compress(httpx.QueryParams(FORM).__str__().encode()),
            "headers": {
                "content-type": "application/x-www-form-urlencoded",
                "content-encoding": "gzip",
            },
        },
        {
            "content": "action=requesttoken&grant_type=authorization_code&code=é".encode(),
            "headers": {"content-type": "application/x-www-form-urlencoded"},
        },
        {"data": {**FORM, "code": "c" * 5000}},  # over 4 KiB
        {
            "content": b"action=requesttoken&grant_type=authorization_code&code=c&&",
            "headers": {"content-type": "application/x-www-form-urlencoded"},
        },
    ],
)
async def test_any_other_body_is_refused(kwargs: dict[str, Any]) -> None:
    recorder = Recorder()
    with pytest.raises(OutboundBlocked):
        await _send(recorder, "POST", WITHINGS_TOKEN_URL, **kwargs)
    assert recorder.seen == []


@pytest.mark.parametrize(
    "location", [WITHINGS_TOKEN_URL, "/v2/oauth2", "https://evil.example/steal"]
)
@pytest.mark.parametrize("follow", [False, True])
async def test_a_redirect_answer_is_refused_and_never_followed(location: str, follow: bool) -> None:
    """The form holds the code and the client secret: no hop gets it, not even the same URL."""
    recorder = Recorder(httpx.Response(307, headers={"Location": location}))
    with pytest.raises(OutboundBlocked) as raised:
        await _send(recorder, "POST", WITHINGS_TOKEN_URL, data=FORM, follow_redirects=follow)
    assert raised.value.after_request is True
    assert len(recorder.seen) == 1


async def test_a_refusal_logs_the_host_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(OutboundBlocked) as raised:
        await _send(Recorder(), "POST", WITHINGS_TOKEN_URL, data={**FORM, "action": "revoke"})
    assert [r.getMessage() for r in caplog.records] == ["outbound request blocked"]
    shown = caplog.text + repr(raised.value) + repr(raised.value.args)
    for secret in ("planted-client-secret", "planted-authorization-code", "revoke"):
        assert secret not in shown
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_the_client_is_unusable_from_the_worker() -> None:
    recorder = Recorder()
    client = withings_code_exchange_client(inner=httpx.MockTransport(recorder))
    install_worker_allowlist()
    with pytest.raises(OutboundBlocked):  # built before, refused per request
        async with client:
            await client.post(WITHINGS_TOKEN_URL, data=FORM)
    with pytest.raises(OutboundBlocked):  # and not built at all
        withings_code_exchange_client(inner=httpx.MockTransport(recorder))
    assert recorder.seen == []


async def test_the_client_has_its_total_deadline_and_no_environment() -> None:
    async with withings_code_exchange_client() as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client._mounts == {}  # pyright: ignore[reportPrivateUsage]
        assert client.timeout == httpx.Timeout(egress.CODE_EXCHANGE_TOTAL_SECONDS)
        transport = client._transport  # pyright: ignore[reportPrivateUsage]
        assert getattr(transport, "_total_seconds") == 10.0  # noqa: B009
        assert getattr(transport, "_allowlist").hosts == {"wbsapi.withings.net"}  # noqa: B009


def test_the_form_check() -> None:
    expected = {"action", "grant_type", "client_id", "client_secret", "code", "redirect_uri"}
    assert expected == CODE_EXCHANGE_FIELDS
    assert is_code_exchange_form(str(httpx.QueryParams(FORM)).encode())
    for body in (b"", b"&", b"=", b"action", b"\xff", b"code=c&" * 10):
        assert not is_code_exchange_form(body)
