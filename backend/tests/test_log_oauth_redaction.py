"""ADR 0009 §10: the OAuth ``code``, ``code_verifier`` and ``state`` never reach a log line.

caplog captures the records and formats them with the production ``JsonFormatter``; each test
plants the three values (public test values) where a log line could carry them and finds none in
the output."""

import json
import logging
import secrets

import pytest

from ownvitals.log import QUIET_LOGGERS, JsonFormatter
from spotifycore import SCOPES, FakeSpotify, challenge_of
from spotifyfake import CALLBACK, CLIENT_A, spotify_client

CODE = "planted-code-" + "C" * 20
VERIFIER = "planted-verifier-" + "V" * 30
STATE = "planted-state-" + "S" * 20
PLANTED = (CODE, VERIFIER, STATE)


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
    logger = logging.getLogger("ownvitals.spotify.test")
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
        f"&client_id={CLIENT_A}&code_verifier={VERIFIER}",
        # The callback URL, under /oauth/ and elsewhere.
        f"GET /oauth/spotify/callback?code={CODE}&state={STATE} 302",
        f"https://ownvitals.example/account/spotify?state={STATE}&code={CODE}",
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
    logger = logging.getLogger("ownvitals.spotify.test")
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


async def test_a_token_exchange_logs_none_of_them(logs: pytest.LogCaptureFixture) -> None:
    """The real client against the fake, every logger at DEBUG: nothing planted in the output,
    whether the exchange succeeds or is refused."""
    fake = FakeSpotify()
    fake.add_user("rider-one")
    fake.add_app(CLIENT_A, redirect_uris=[CALLBACK], allowed=["rider-one"])
    fake.sign_in("rider-one")
    verifier = "planted-verifier-" + secrets.token_urlsafe(32)
    authorize = fake.handle(
        "GET",
        "https://accounts.spotify.com/authorize?"
        + "&".join(
            f"{k}={v}"
            for k, v in {
                "client_id": CLIENT_A,
                "response_type": "code",
                "redirect_uri": CALLBACK,
                "scope": SCOPES.replace(" ", "+"),
                "state": STATE,
                "code_challenge_method": "S256",
                "code_challenge": challenge_of(verifier),
            }.items()
        ),
        {},
        None,
    )
    code = authorize.headers["Location"].split("code=")[1].split("&")[0]
    async with spotify_client(fake) as client:
        for given in (verifier, "x" * 43):  # refused the second time: the code is used
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK,
                "client_id": CLIENT_A,
                "code_verifier": given,
            }
            answer = await client.post("https://accounts.spotify.com/api/token", data=form)
            try:
                answer.raise_for_status()
            except Exception:
                logging.getLogger("ownvitals.spotify.test").exception("exchange %s", form)
    assert logs.records  # the failure was logged
    for value in (code, verifier, STATE):
        assert value not in logs.text
