"""The JSON formatter's DBAPIError redaction walks the exception chain (review of #46)."""

import io
import json
import logging

import pytest
from fastapi import BackgroundTasks, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError, OperationalError

from ownvitals.log import configure_logging, find_database_error, redact

SECRET = "victim@example.com"


class FakeDriverError(Exception):
    sqlstate = "23505"


def database_error() -> IntegrityError:
    orig = FakeDriverError(f"Key (email_normalized)=({SECRET}) already exists.")
    return IntegrityError("INSERT ...", {"p": SECRET}, orig)


def logged(error: BaseException) -> str:
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    try:
        raise error
    except BaseException:
        logging.getLogger("ownvitals.test").exception("failed")
    line = json.loads(stream.getvalue().splitlines()[-1])
    return line["exc"]


@pytest.mark.usefixtures("restore_logging")
def test_a_direct_database_error_is_class_and_sqlstate_only() -> None:
    text = logged(database_error())
    assert text == "IntegrityError (SQLSTATE 23505)"


@pytest.mark.usefixtures("restore_logging")
def test_the_cause_chain_is_walked() -> None:
    try:
        raise database_error()
    except IntegrityError as exc:
        wrapped = RuntimeError("Caught handled exception, but response already started.")
        wrapped.__cause__ = exc
    text = logged(wrapped)
    assert SECRET not in text
    assert "SQLSTATE 23505" in text
    assert "RuntimeError" in text


@pytest.mark.usefixtures("restore_logging")
def test_the_context_chain_is_walked() -> None:
    try:
        try:
            raise database_error()
        except IntegrityError:
            raise ValueError("while handling")  # noqa: B904 - an implicit __context__
    except ValueError as exc:
        text = logged(exc)
    assert SECRET not in text
    assert "SQLSTATE 23505" in text


@pytest.mark.usefixtures("restore_logging")
def test_a_suppressed_context_is_not_followed() -> None:
    """``raise ... from None`` hides the context on purpose (a library error holding a ticket);
    the formatter must not dig it out again."""
    try:
        try:
            raise database_error()
        except IntegrityError:
            raise ValueError("while handling") from None
    except ValueError as exc:
        caught = exc
    assert caught.__context__ is not None  # still there; suppressed only
    assert find_database_error(caught) is None
    text = logged(caught)
    assert SECRET not in text
    assert "SQLSTATE" not in text
    assert "ValueError: while handling" in text


@pytest.mark.usefixtures("restore_logging")
def test_an_explicit_cause_is_still_followed_when_the_context_is_suppressed() -> None:
    try:
        try:
            raise database_error()
        except IntegrityError as db_error:
            raise RuntimeError("wrapped") from db_error  # sets __suppress_context__ too
    except RuntimeError as exc:
        caught = exc
    assert caught.__suppress_context__ is True
    text = logged(caught)
    assert SECRET not in text
    assert text == "RuntimeError caused by IntegrityError (SQLSTATE 23505)"


@pytest.mark.usefixtures("restore_logging")
def test_exception_group_members_are_walked() -> None:
    group = ExceptionGroup(
        "task group", [ValueError("x"), OperationalError("s", {}, Exception(SECRET))]
    )
    text = logged(group)
    assert SECRET not in text
    assert "OperationalError" in text


def test_a_cycle_in_the_chain_ends() -> None:
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert find_database_error(a) is None


def test_other_errors_keep_their_traceback(restore_logging: None) -> None:
    assert "ValueError: boom" in logged(ValueError("boom"))


def test_tokens_in_urls_are_redacted() -> None:
    assert "abc123" not in redact("opened https://x.test/reset#token=abc123 today")
    assert "abc123" not in redact("GET /x?token=abc123&y=1")


@pytest.mark.usefixtures("restore_logging")
async def test_a_database_error_raised_in_a_background_task_is_redacted() -> None:
    """Starlette re-raises an error after the response started; the formatter must still
    find the DBAPIError under it."""
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    app = FastAPI()

    async def task() -> None:
        raise database_error()

    @app.post("/boom")
    async def boom(background: BackgroundTasks) -> dict[str, bool]:
        background.add_task(task)
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        try:
            await client.post("/boom")
        except Exception:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    out = stream.getvalue()
    assert "Exception in ASGI application" in out
    assert SECRET not in out
    assert "SQLSTATE 23505" in out


@pytest.mark.usefixtures("restore_logging")
def test_secret_keys_are_masked_anywhere_in_a_record() -> None:
    """Token, password, ticket, webhook-secret and connection-row fields by key name."""
    stream = io.StringIO()
    configure_logging("DEBUG", stream)
    planted = {
        "token": "tok-11111111",
        "access_token": "at-22222222",
        "refresh_token": "rt-33333333",
        "password": "pw-44444444",
        "secret": "sc-55555555",
        "Authorization": "Bearer au-66666666",
        "cookie": "ck-77777777",
        "ticket": "LT-88888888",
        "public": {"client_id": "pub-99999999"},
        "nested": {"row": {"wrapped_dek": "wd-00000000", "secret_nonce": "sn-aaaaaaaa"}},
        "listed": [{"webhook_secret": "ws-bbbbbbbb"}],
    }
    logging.getLogger("ownvitals.test").warning("connection", extra={"connection": planted})
    out = stream.getvalue()
    for marker in (
        "1111",
        "2222",
        "3333",
        "4444",
        "5555",
        "6666",
        "7777",
        "8888",
        "9999",
        "0000",
        "aaaa",
        "bbbb",
    ):
        assert marker not in out
    line = json.loads(out.splitlines()[-1])
    assert line["connection"]["public"] == "[redacted]"
    assert line["connection"]["nested"]["row"]["wrapped_dek"] == "[redacted]"


def test_the_provider_and_http_loggers_are_quiet(restore_logging: None) -> None:
    configure_logging("DEBUG", io.StringIO())
    for name in ("anthropic", "httpx", "httpcore", "urllib3"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
