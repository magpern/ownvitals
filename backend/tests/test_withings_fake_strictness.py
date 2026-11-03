"""Fake follow-ups of the #71 review and the e2e-only callback opt-out (#36): case-blind log
masking, ``make_fake(rate_limit=...)``, a ``;`` query separator, a form body without its media
type, stricter callback hosts, and the lenient callback mode (default strict). Fakes only.
"""

from urllib.parse import urlencode

import pytest

from withingscore import FakeWithings
from withingsfake import make_fake
from withingsrules import callback_url_problem
from withingswire import (
    API_HOST,
    STATUS_BAD_CALLBACK,
    STATUS_BAD_PARAMS,
    STATUS_RATE_LIMITED,
    Request,
)

FORM = {"content-type": "application/x-www-form-urlencoded"}
HOOK = "https://app.example.test/api/webhooks/withings/synthetic-secret"


def _post(fake: FakeWithings, path: str, token: str | None = None, query: str = "", **form: object):
    headers = dict(FORM)
    if token:
        headers["authorization"] = f"Bearer {token}"
    body = urlencode({k: str(v) for k, v in form.items()}).encode()
    return fake.handle(Request("POST", f"https://{API_HOST}{path}{query}", headers, body))


def test_the_log_masks_a_secret_whatever_the_case_of_its_name() -> None:
    fake, app, _ = make_fake()
    reply = _post(
        fake, "/v2/oauth2", query="?Client_Secret=leaked-value", action="requesttoken",
        client_id=app.client_id, grant_type="authorization_code",
    )  # fmt: skip
    assert reply.json()["status"] == STATUS_BAD_PARAMS
    assert fake.log[-1].params["Client_Secret"] == "***"
    assert "leaked-value" not in repr(fake.log)


def test_make_fake_passes_the_rate_limit() -> None:
    limited, app, _ = make_fake(rate_limit=2, days=2, ecg_count=0)
    statuses = [
        _post(limited, "/v2/oauth2", action="requesttoken", client_id=app.client_id).json()[
            "status"
        ]
        for _ in range(3)
    ]
    assert statuses[-1] == STATUS_RATE_LIMITED
    free, app, _ = make_fake(rate_limit=None, days=2, ecg_count=0)
    for _ in range(130):
        reply = _post(free, "/v2/oauth2", action="requesttoken", client_id=app.client_id)
        assert reply.json()["status"] != STATUS_RATE_LIMITED


def test_a_semicolon_in_the_query_string_is_refused() -> None:
    fake, app, _ = make_fake()
    reply = _post(fake, "/v2/oauth2", query="?a=1;client_secret=x", action="requesttoken",
                  client_id=app.client_id)  # fmt: skip
    assert reply.json()["status"] == STATUS_BAD_PARAMS


@pytest.mark.parametrize(
    "headers", [{}, {"content-type": "application/json"}, {"content-type": "text/plain"}]
)
def test_a_form_body_without_the_form_media_type_is_refused(headers: dict[str, str]) -> None:
    fake, app, _ = make_fake()
    body = urlencode({"action": "requesttoken", "client_id": app.client_id}).encode()
    reply = fake.handle(Request("POST", f"https://{API_HOST}/v2/oauth2", headers, body))
    assert reply.json()["status"] == STATUS_BAD_PARAMS


def test_the_form_media_type_may_carry_a_charset() -> None:
    fake, app, _ = make_fake()
    headers = {"content-type": "Application/X-WWW-Form-Urlencoded; charset=utf-8"}
    body = urlencode({"action": "requesttoken", "client_id": app.client_id}).encode()
    reply = fake.handle(Request("POST", f"https://{API_HOST}/v2/oauth2", headers, body))
    assert "form-urlencoded" not in reply.json().get("error", "")


REFUSED_HOSTS = {
    "userinfo": "https://user@app.example.test/hook",
    "userinfo with a password": "https://user:pw@app.example.test/hook",
    "an @ in the path-less host": "https://app.example.test@evil.example/hook",
    ".local": "https://printer.local/hook",
    ".internal": "https://app.internal/hook",
    "an underscore": "https://my_app.example.test/hook",
    "a space in the host": "https://my app.example.test/hook",
    "leading whitespace": " https://app.example.test/hook",
    "trailing whitespace": "https://app.example.test/hook ",
    "a newline": "https://app.example.test/hook\n",
}


@pytest.mark.parametrize("url", REFUSED_HOSTS.values(), ids=REFUSED_HOSTS.keys())
def test_these_callback_hosts_are_refused(url: str) -> None:
    assert callback_url_problem(url) is not None


def test_the_default_refuses_the_compose_receiver_and_the_opt_out_takes_it() -> None:
    compose_hook = "http://web:8000/api/webhooks/withings/synthetic-secret"
    assert callback_url_problem(compose_hook) == "not HTTPS"
    assert callback_url_problem(compose_hook, strict=False) is None
    assert callback_url_problem("ftp://web/hook", strict=False) is not None
    assert callback_url_problem("http:///hook", strict=False) is not None
    assert FakeWithings().strict_callbacks is True
    strict, app, user = make_fake(days=2, ecg_count=0)
    lenient = FakeWithings(strict_callbacks=False)
    for fake, expected in ((strict, STATUS_BAD_CALLBACK), (lenient, 0)):
        if fake is lenient:
            app = fake.add_app()
            user = fake.add_user(days=2, ecg_count=0)
        token = fake.tokens_for(app.client_id, user.userid)["access_token"]
        reply = _post(fake, "/notify", token, action="subscribe", callbackurl=compose_hook, appli=1)
        assert reply.json()["status"] == expected
