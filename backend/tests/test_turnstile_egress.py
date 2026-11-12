"""Turnstile's one outbound request (``egress.turnstile``, roadmap Amendment 12, ADR 0018).

Every request here goes to an ``httpx.MockTransport`` under the package's checks: no network.
The secret and the tokens are synthetic.
"""

import urllib.request
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from ownvitals import egress
from ownvitals.egress import (
    OutboundBlocked,
    core,
    encode_turnstile_form,
    install_worker_allowlist,
    is_turnstile_form,
    verify_turnstile_token,
)
from ownvitals.egress import turnstile as egress_turnstile

SECRET = "synthetic-turnstile-secret"
TOKEN = "synthetic.token-0_1"
URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


def recorder(
    seen: list[httpx.Request], answer: httpx.Response | None = None
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer if answer is not None else httpx.Response(200, json={"success": True})

    return httpx.MockTransport(handle)


async def test_it_sends_exactly_one_fixed_post() -> None:
    seen: list[httpx.Request] = []
    response = await verify_turnstile_token(secret=SECRET, token=TOKEN, inner=recorder(seen))
    assert response.status_code == 200
    [request] = seen
    assert (request.method, str(request.url)) == ("POST", URL)
    assert request.content == urlencode([("secret", SECRET), ("response", TOKEN)]).encode()
    assert {
        name.lower() for name, _ in request.headers.multi_items()
    } <= egress_turnstile.TURNSTILE_HEADERS
    assert b"remoteip" not in request.content  # the visitor's address is not sent


@pytest.mark.parametrize(
    "token",
    ["", " ", "a b", "a;b", "a&b", "a=b", "a#b", "a\nb", "a\rb", "ä", "x" * 2049, None, 5],
)
async def test_a_token_egress_will_not_send_is_refused_before_anything_leaves(token: Any) -> None:
    seen: list[httpx.Request] = []
    with pytest.raises(OutboundBlocked) as raised:
        await verify_turnstile_token(secret=SECRET, token=token, inner=recorder(seen))
    assert raised.value.host == "challenges.cloudflare.com"
    assert seen == []


async def test_a_bad_secret_is_refused_too() -> None:
    for secret in ("", "s p", "s&t", "x" * 257):
        with pytest.raises(OutboundBlocked):
            encode_turnstile_form(secret=secret, token=TOKEN)


def test_the_form_check_admits_exactly_the_two_fields() -> None:
    good = urlencode([("secret", SECRET), ("response", TOKEN)]).encode()
    assert is_turnstile_form(good)
    for body in (
        urlencode([("response", TOKEN), ("secret", SECRET)]).encode(),  # order
        urlencode([("secret", SECRET)]).encode(),
        good + b"&remoteip=203.0.113.5",
        good + b"&secret=x",
        b"secret=a%3Bb&response=t",  # a forbidden character once decoded
        b"secret=s&response=t;x=1",
        b"secret=s&response=%E4",
        b"x" * 5000,
    ):
        assert not is_turnstile_form(body), body


async def test_the_transport_refuses_any_other_request_on_the_wire() -> None:
    seen: list[httpx.Request] = []
    transport = egress_turnstile._TurnstileTransport(  # pyright: ignore[reportPrivateUsage]
        egress.TURNSTILE_ALLOWLIST, recorder(seen), total_seconds=5.0, role="web"
    )
    good = urlencode([("secret", SECRET), ("response", TOKEN)]).encode()
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(transport=transport) as client:
        for method, url, body, headers in (
            ("GET", URL, b"", {}),
            ("POST", URL + "?x=1", good, form),
            ("POST", "https://challenges.cloudflare.com/turnstile/v0/api.js", good, form),
            ("POST", URL, good + b"&remoteip=1", form),
            ("POST", URL, good, {**form, "Cookie": "a=b"}),
            ("POST", URL, good, {"Content-Type": "application/json"}),
            ("POST", "https://evil.example/turnstile/v0/siteverify", good, form),
            ("POST", "http://challenges.cloudflare.com/turnstile/v0/siteverify", good, form),
        ):
            with pytest.raises(OutboundBlocked):
                await client.request(method, url, content=body, headers=headers)
    assert seen == []


async def test_a_redirect_is_refused_never_followed() -> None:
    seen: list[httpx.Request] = []
    moved = httpx.Response(302, headers={"Location": "https://challenges.cloudflare.com/other"})
    with pytest.raises(OutboundBlocked):
        await verify_turnstile_token(secret=SECRET, token=TOKEN, inner=recorder(seen, moved))
    assert len(seen) == 1


async def test_it_is_refused_in_the_worker() -> None:
    install_worker_allowlist(ai_enabled=True)
    seen: list[httpx.Request] = []
    with pytest.raises(OutboundBlocked) as raised:
        await verify_turnstile_token(secret=SECRET, token=TOKEN, inner=recorder(seen))
    assert raised.value.host == "challenges.cloudflare.com"
    assert seen == []


async def test_nothing_is_logged_but_the_host(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(OutboundBlocked):
        await verify_turnstile_token(secret=SECRET, token="bad token", inner=recorder([]))
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert SECRET not in text
    assert "bad token" not in text
