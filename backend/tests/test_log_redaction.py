"""The OAuth ``code``, ``code_verifier`` and ``state``, session cookies and the secret part of a
webhook path never reach a log line.

caplog captures the records and formats them with the production ``JsonFormatter``; each test
plants synthetic values where a log line could carry them and finds none in the output."""

import json
import logging

import pytest

from ownvitals.log import QUIET_LOGGERS, JsonFormatter, redact

CODE = "planted-code-" + "C" * 20
VERIFIER = "planted-verifier-" + "V" * 30
STATE = "planted-state-" + "S" * 20
PLANTED = (CODE, VERIFIER, STATE)
WEBHOOK_SECRET = "planted-webhook-secret-" + "W" * 24
SESSION = "planted-session-" + "X" * 24


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.handler.setFormatter(JsonFormatter())
    caplog.set_level(logging.DEBUG)
    for name in QUIET_LOGGERS:  # caplog sets the root; the libraries keep their own levels
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


def _assert_none_planted(text: str) -> None:
    for value in PLANTED:
        assert value not in text
    # The distinctive tails too, so no partial value slips through either.
    for tail in ("C" * 20, "V" * 30, "S" * 20):
        assert tail not in text


def test_extra_fields_named_code_verifier_and_state_are_masked(
    logs: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("ownvitals.auth.test")
    logger.warning("callback", extra={"code": CODE, "code_verifier": VERIFIER, "state": STATE})
    logger.warning(
        "nested", extra={"flow": {"Code": CODE, "STATE": STATE, "Code_Verifier": [VERIFIER]}}
    )
    logger.warning("list", extra={"forms": [{"code_verifier": VERIFIER}, {"code": CODE}]})
    _assert_none_planted(logs.text)
    assert len(logs.records) == 3
    lines = [json.loads(line) for line in logs.text.splitlines()]
    assert lines[0]["code"] == lines[0]["state"] == "[redacted]"


@pytest.mark.parametrize(
    "text",
    [
        # The token request body, as an exception or a library might show it.
        f"grant_type=authorization_code&code={CODE}&redirect_uri=http%3A%2F%2F127.0.0.1"
        f"&client_id=synthetic-client&code_verifier={VERIFIER}",
        # The callback URL, under /oauth/ and elsewhere.
        f"GET /oauth/google/callback?code={CODE}&state={STATE} 302",
        f"https://ownvitals.example/account/connect?state={STATE}&code={CODE}",
        f"redirect to http://127.0.0.1:8000/x?code={CODE}#state={STATE}",
        # JSON or a dict's repr.
        json.dumps({"code": CODE, "code_verifier": VERIFIER, "state": STATE}),
        repr({"code": CODE, "code_verifier": VERIFIER, "state": STATE}),
        f"code={CODE} code_verifier={VERIFIER} state={STATE}",
        # A keyword-argument or dataclass repr: the value is quoted.
        f"Req(code='{CODE}', code_verifier=\"{VERIFIER}\", state='{STATE}')",
        f'code=\'{CODE}\' code_verifier="{VERIFIER}" state="{STATE}"',
        # A token endpoint's JSON body inside an exception message.
        json.dumps({"access_token": CODE, "refresh_token": VERIFIER, "client_secret": STATE}),
    ],
)
def test_query_form_and_json_text_is_masked(logs: pytest.LogCaptureFixture, text: str) -> None:
    logger = logging.getLogger("ownvitals.auth.test")
    logger.warning(text)
    logger.warning("formatted %s", text)
    logger.warning("in a field", extra={"detail": text})
    try:
        raise ValueError(text)
    except ValueError:
        logger.exception("failed")
    _assert_none_planted(logs.text)
    assert len(logs.records) == 4


def test_a_job_state_is_still_logged(logs: pytest.LogCaptureFixture) -> None:
    """``state`` is masked, so the job runner logs a job's state as ``job_state``."""
    logging.getLogger("ownvitals.jobs.runner").info("ended", extra={"job_state": "completed"})
    assert json.loads(logs.text)["job_state"] == "completed"


@pytest.mark.parametrize(
    "text",
    [
        f"POST /api/webhooks/withings/{WEBHOOK_SECRET} 200",
        f"HEAD /api/webhooks/withings/{WEBHOOK_SECRET}?x=1 200",
        f"https://withings.example/api/webhooks/withings/{WEBHOOK_SECRET}",
        f"Not Found: /api/webhooks/some-other-provider/{WEBHOOK_SECRET}#frag",
        f"{{'path': '/api/webhooks/withings/{WEBHOOK_SECRET}'}}",
        f'"url": "/api/webhooks/withings/{WEBHOOK_SECRET}"',
    ],
)
def test_the_secret_part_of_a_webhook_path_is_masked(
    logs: pytest.LogCaptureFixture, text: str
) -> None:
    logger = logging.getLogger("ownvitals.test")
    logger.warning(text)
    logger.warning("formatted %s", text)
    logger.warning("in a field", extra={"detail": text})
    try:
        raise ValueError(text)
    except ValueError:
        logger.exception("failed")
    assert WEBHOOK_SECRET not in logs.text
    assert "W" * 24 not in logs.text
    assert "/api/webhooks/" in logs.text  # the provider part stays: it says which webhook it was
    assert len(logs.records) == 4


def test_the_webhook_path_pattern_keeps_the_rest_of_the_line() -> None:
    line = f"POST /api/webhooks/withings/{WEBHOOK_SECRET} status=200"
    assert redact(line) == "POST /api/webhooks/withings/[redacted] status=200"
    assert redact("GET /api/health 200") == "GET /api/health 200"


def test_a_webhook_secret_field_is_masked_by_name(logs: pytest.LogCaptureFixture) -> None:
    logging.getLogger("ownvitals.test").warning(
        "stored", extra={"webhook_secret": WEBHOOK_SECRET, "row": {"webhook_secret": "w-1234"}}
    )
    assert "planted-webhook" not in logs.text
    assert "w-1234" not in logs.text


@pytest.mark.parametrize(
    "text",
    [
        f"Cookie: __Host-ownvitals_session={SESSION}; other=1",
        f"set-cookie: __Host-ownvitals_session={SESSION}; Path=/; Secure",
        f"{{'__Host-ownvitals_session': '{SESSION}'}}",
        f"Authorization: Bearer {SESSION}",
        f"X-CSRF-Token: {SESSION}",
    ],
)
def test_session_cookies_and_credential_headers_are_masked(
    logs: pytest.LogCaptureFixture, text: str
) -> None:
    logging.getLogger("ownvitals.test").warning("request: %s", text)
    assert SESSION not in logs.text
    assert "X" * 24 not in logs.text


# --- the secret part of a webhook path in its encoded forms ------------------------------------

ENCODED_WEBHOOK_TEXTS = [
    # Percent-encoded: the last slash, or all of them (a path inside a query value).
    f"GET /api/webhooks/withings%2F{WEBHOOK_SECRET} 200",
    f"GET /api/webhooks/withings%2f{WEBHOOK_SECRET} 200",
    f"redirect_to=%2Fapi%2Fwebhooks%2Fwithings%2F{WEBHOOK_SECRET}&x=1",
    f"redirect_to=%2fapi%2fwebhooks%2fwithings%2f{WEBHOOK_SECRET}",
    f"redirect_to=%252Fapi%252Fwebhooks%252Fwithings%252F{WEBHOOK_SECRET}",
    # JSON with escaped slashes.
    f'{{"path": "\\/api\\/webhooks\\/withings\\/{WEBHOOK_SECRET}"}}',
    f'{{"path": "\\/api\\/webhooks\\/withings\\/{WEBHOOK_SECRET}\\/more", "n": 1}}',
    # Another case and a mix.
    f"GET /API/Webhooks/Withings/{WEBHOOK_SECRET}",
    f"GET \\/api/webhooks%2Fwithings\\/{WEBHOOK_SECRET}",
]


@pytest.mark.parametrize("text", ENCODED_WEBHOOK_TEXTS)
def test_encoded_and_escaped_webhook_paths_are_masked(
    logs: pytest.LogCaptureFixture, text: str
) -> None:
    redacted = redact(text)
    assert WEBHOOK_SECRET not in redacted
    assert "W" * 24 not in redacted
    assert "[redacted]" in redacted
    logger = logging.getLogger("ownvitals.test")
    logger.warning(text)
    logger.warning("formatted %s", text)
    logger.warning("in a field", extra={"detail": text, "nested": {"url": text}})
    assert "W" * 24 not in logs.text
    assert len(logs.records) == 3


def test_the_encoded_pattern_keeps_text_around_the_secret() -> None:
    assert redact(f"a=1&u=%2Fapi%2Fwebhooks%2Fwithings%2F{WEBHOOK_SECRET}&b=2") == (
        "a=1&u=%2Fapi%2Fwebhooks%2Fwithings%2F[redacted]&b=2"
    )
    assert redact("see /api/webhooks for the list") == "see /api/webhooks for the list"
    assert redact("GET /api/health 200") == "GET /api/health 200"


# --- dict keys are redacted like values --------------------------------------------------------

SECRET_TEXT_KEYS = {
    "a webhook path": f"/api/webhooks/withings/{WEBHOOK_SECRET}",
    "a cookie": f"__Host-ownvitals_session={SESSION}",
    "an oauth query": f"code={CODE}",
    "a token in a link": f"https://ownvitals.example/reset#token={SESSION}",
}


@pytest.mark.parametrize("key", SECRET_TEXT_KEYS.values(), ids=SECRET_TEXT_KEYS.keys())
def test_a_secret_used_as_a_nested_dict_key_is_masked(
    logs: pytest.LogCaptureFixture, key: str
) -> None:
    logger = logging.getLogger("ownvitals.test")
    logger.warning("hits", extra={"by_key": {key: 3}})
    logger.warning("deeper", extra={"a": {"b": [{key: 1}, ({key: 2},)]}})
    for planted in (WEBHOOK_SECRET, SESSION, CODE):
        assert planted not in logs.text
    for tail in ("W" * 24, "X" * 24, "C" * 20):
        assert tail not in logs.text
    assert len(logs.records) == 2
    first = json.loads(logs.text.splitlines()[0])
    assert list(first["by_key"].values()) == [3]  # the value is kept, the key is redacted


@pytest.mark.parametrize("key", SECRET_TEXT_KEYS.values(), ids=SECRET_TEXT_KEYS.keys())
def test_a_secret_used_as_a_top_level_extra_key_is_masked(
    logs: pytest.LogCaptureFixture, key: str
) -> None:
    logging.getLogger("ownvitals.test").warning("hit", extra={key: 1})
    for planted in (WEBHOOK_SECRET, SESSION, CODE):
        assert planted not in logs.text
    for tail in ("W" * 24, "X" * 24, "C" * 20):
        assert tail not in logs.text
    line = json.loads(logs.text)
    assert line["message"] == "hit"
    if not key.startswith("_"):  # a field named with a leading underscore is never logged
        assert 1 in line.values()


def test_keys_that_hold_no_secret_are_kept_as_they_are(logs: pytest.LogCaptureFixture) -> None:
    logging.getLogger("ownvitals.test").warning(
        "ok", extra={"route": "/api/health", "counts": {"/api/health": 4, "get": 2}}
    )
    line = json.loads(logs.text)
    assert line["route"] == "/api/health"
    assert line["counts"] == {"/api/health": 4, "get": 2}
