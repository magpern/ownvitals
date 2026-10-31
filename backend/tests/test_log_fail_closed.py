"""When building or writing a log line fails, the secret still goes nowhere.

The stdlib's ``Handler.handleError`` prints a failing record's raw message and arguments to
stderr, which is the container log. Each test plants a synthetic secret where that would happen
and checks the line, stdout and stderr.

``configure_logging`` runs inside each test, not in a fixture: it replaces the root logger's
handlers, and pytest adds its own capture handlers for the test's call phase (their formatter
raises on a bad record, which is pytest's business, not the subject here)."""

import io
import json
import logging

import pytest

from ownvitals.log import LAST_RESORT_LINE, JsonFormatter, configure_logging

SECRET = "planted-webhook-secret-" + "W" * 24
PATH = f"/api/webhooks/withings/{SECRET}"
LOGGER = "ownvitals.test"


def assert_nowhere(
    capsys: pytest.CaptureFixture[str], *streams: io.StringIO, expect_stderr: str = ""
) -> None:
    captured = capsys.readouterr()
    for text in (captured.out, captured.err, *(s.getvalue() for s in streams)):
        assert SECRET not in text
        assert "W" * 24 not in text
    assert captured.err == expect_stderr


class Unprintable:
    """A value whose ``str`` raises, with the secret in the error text."""

    def __str__(self) -> str:
        raise RuntimeError(f"cannot print {PATH}")


class BrokenStream(io.StringIO):
    """A stream that fails every write, with the line it was given in the error text."""

    def write(self, text: str, /) -> int:
        raise OSError(f"disk full while writing {text}")


def logging_to_a_string() -> io.StringIO:
    out = io.StringIO()
    configure_logging("DEBUG", out)
    return out


def only_line(stream: io.StringIO) -> dict[str, object]:
    (line,) = stream.getvalue().splitlines()
    return json.loads(line)


@pytest.mark.usefixtures("restore_logging")
def test_a_wrong_number_of_arguments_keeps_the_secret_out_of_every_stream(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = logging_to_a_string()
    logging.getLogger(LOGGER).info("two placeholders %s %s", PATH)
    line = only_line(stream)
    assert line["message"] == "two placeholders %s %s"
    assert line["log_error"] == "TypeError"
    assert line["level"] == "INFO"
    assert line["logger"] == LOGGER
    assert_nowhere(capsys, stream)


@pytest.mark.usefixtures("restore_logging")
def test_the_message_text_of_a_failed_record_is_redacted_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = logging_to_a_string()
    logging.getLogger(LOGGER).info(f"POST {PATH} %s %s", "one")
    assert only_line(stream)["message"] == "POST /api/webhooks/withings/[redacted] %s %s"
    assert_nowhere(capsys, stream)


@pytest.mark.usefixtures("restore_logging")
def test_a_circular_structure_in_the_extra_fields_is_not_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = logging_to_a_string()
    loop: list[object] = [PATH]
    loop.append(loop)
    logging.getLogger(LOGGER).warning("cyclic", extra={"loop": loop, "path": PATH})
    line = only_line(stream)
    assert line["message"] == "cyclic"
    assert line["log_error"] == "RecursionError"
    assert "loop" not in line  # the extra fields are dropped with the failure
    assert_nowhere(capsys, stream)


@pytest.mark.usefixtures("restore_logging")
def test_a_value_that_cannot_be_printed_is_not_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = logging_to_a_string()
    logging.getLogger(LOGGER).warning("bad value", extra={"value": Unprintable()})
    line = only_line(stream)
    assert (line["message"], line["log_error"]) == ("bad value", "RuntimeError")
    assert_nowhere(capsys, stream)


@pytest.mark.usefixtures("restore_logging")
def test_a_message_that_cannot_be_printed_becomes_a_marker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = logging_to_a_string()
    logging.getLogger(LOGGER).warning(Unprintable())  # pyright: ignore[reportArgumentType]
    line = only_line(stream)
    assert (line["message"], line["log_error"]) == ("[unprintable]", "RuntimeError")
    assert_nowhere(capsys, stream)


@pytest.mark.usefixtures("restore_logging")
def test_a_tuple_key_in_an_extra_dict_is_written_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = logging_to_a_string()
    logging.getLogger(LOGGER).warning("hits", extra={"by_path": {(PATH, 1): 3}})
    line = only_line(stream)
    assert line["message"] == "hits"
    assert line["by_path"] == {"('/api/webhooks/withings/[redacted]', 1)": 3}
    assert_nowhere(capsys, stream)


@pytest.mark.usefixtures("restore_logging")
def test_a_failing_write_reports_only_the_error_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert logging.raiseExceptions  # the stdlib default that prints the raw record
    configure_logging("DEBUG", BrokenStream())
    logger = logging.getLogger(LOGGER)
    logger.warning(f"POST {PATH}")
    logger.warning("POST %s", PATH)
    logger.warning("with extra", extra={"path": PATH})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err
    assert "W" * 24 not in captured.err
    lines = [json.loads(line) for line in captured.err.splitlines()]
    assert len(lines) == 3
    assert {line["message"] for line in lines} == {"a log record could not be written"}
    assert {line["log_error"] for line in lines} == {"OSError"}


def test_the_formatter_never_raises_even_when_the_fallback_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = logging.LogRecord(LOGGER, logging.INFO, __file__, 1, PATH + " %s", (), None)

    def broken(*_: object, **__: object) -> str:
        raise ValueError(PATH)

    monkeypatch.setattr("ownvitals.log.json.dumps", broken)
    assert JsonFormatter().format(record) == LAST_RESORT_LINE
    assert SECRET not in LAST_RESORT_LINE
