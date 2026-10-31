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
