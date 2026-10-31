import io
import json
import logging

import pytest

from ownvitals.log import configure_logging


@pytest.mark.usefixtures("restore_logging")
def test_one_json_object_per_line() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logger = logging.getLogger("ownvitals.test")
    logger.info("first\nwith a newline")
    logger.warning("second %s", "message", extra={"error_type": "OSError", "attempt": 2})
    logger.debug("not shown at INFO")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["message"] == "first\nwith a newline"
    assert first["level"] == "INFO"
    assert first["logger"] == "ownvitals.test"
    assert first["ts"].endswith("+00:00")
    assert second["message"] == "second message"
    assert second["level"] == "WARNING"
    assert second["error_type"] == "OSError"
    assert second["attempt"] == 2


@pytest.mark.usefixtures("restore_logging")
def test_exception_is_one_json_field() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("ownvitals.test").exception("failed")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert "ValueError: boom" in record["exc"]


@pytest.mark.usefixtures("restore_logging")
def test_extra_cannot_replace_standard_fields() -> None:
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("ownvitals.test").info("hello", extra={"level": "FAKE", "ts": "never"})
    record = json.loads(stream.getvalue())
    assert record["level"] == "INFO"
    assert record["ts"] != "never"


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_replaces_handlers_and_sets_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG")
    configure_logging("WARNING")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.WARNING
    logging.getLogger("ownvitals.test").warning("to stdout")
    out = capsys.readouterr().out
    assert json.loads(out)["message"] == "to stdout"


@pytest.mark.usefixtures("restore_logging")
def test_a_model_api_key_is_redacted_everywhere() -> None:
    """The key never reaches a log line, whatever carries it."""
    key = "sk-ant-api03-planted-test-value-0123456789abcdef"
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logger = logging.getLogger("ownvitals.test")
    logger.warning("request failed with %s", key)
    logger.warning("headers", extra={"headers": f"x-api-key: {key}"})
    logger.warning("fields", extra={"api_key": key, "nested": {"anthropic_api_key": key}})
    logger.warning("header map", extra={"sent": {"X-Api-Key": key}})
    output = stream.getvalue()
    assert key not in output
    assert "planted-test-value" not in output
    assert len(output.splitlines()) == 4
