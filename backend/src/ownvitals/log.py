"""Logging: stdlib ``logging``, one JSON object per line on stdout."""

import contextlib
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import IO, Any

from sqlalchemy.exc import DBAPIError

_STANDARD_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


# Never logged: the session, device, CSRF, OAuth and pending cookies; the Authorization and
# X-CSRF-Token headers (and whole Cookie headers); query strings on /oauth/*; the secret part of
# a webhook path. Our own access log never records any of them; this catches the rest (an
# exception message or a library that dumps a request).
_REDACTED = "[redacted]"
# A path separator as it may appear in a log line: plain, percent-encoded, or JSON-escaped.
_SEP = r"(?:/|\\/|%(?:25)?2[Ff])"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(__Host-ownvitals_\w+)\s*=\s*[^;\s\"',]+"), rf"\1={_REDACTED}"),
    # A dict or JSON repr: {'__Host-ownvitals_session': 'value'} or "name": "value".
    (re.compile(r"(__Host-ownvitals_\w+[\"']?\s*:\s*[\"']?)[^\"',;}\s]+"), rf"\1{_REDACTED}"),
    (
        re.compile(
            r"(?i)\b(authorization|x-csrf-token|x-api-key|cookie|set-cookie)\b"
            r"([\"']?\s*[:=]\s*[\"']?)[^\r\n\"']*"
        ),
        rf"\1\2{_REDACTED}",
    ),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), f"Bearer {_REDACTED}"),
    # An Anthropic API key (AI insights, opt-in), wherever it shows up.
    (re.compile(r"\bsk-ant-[\w-]+"), _REDACTED),
    # A webhook's path carries its secret: ``/api/webhooks/<provider>/<secret>``. Everything after
    # the provider goes, in a URL, a log line or an exception message, whether the slashes are
    # plain, percent-encoded (``%2F``, also doubly: ``%252F``) or JSON-escaped (a backslash
    # before each slash).
    (
        re.compile(rf"(?i)(api{_SEP}webhooks{_SEP}[\w-]+{_SEP})[^\s?&\"'#\\]+"),
        rf"\1{_REDACTED}",
    ),
    (re.compile(r"(/oauth/[^\s?\"'#]*)\?[^\s\"'#]*"), r"\1"),
    # Mail links carry their token in the fragment: ``/reset#token=...``.
    (re.compile(r"(?i)([#?&]token=)[\w.~%-]+"), rf"\1{_REDACTED}"),
    # The OAuth ``code``, ``code_verifier`` and ``state`` (and the token endpoint's other
    # secrets) in a query or form string, wherever it is (a callback URL, a token request body
    # in an exception), and as a quoted key in a dict or JSON text.
    (
        re.compile(
            r"(?i)(?<![\w-])(code|code_verifier|state|refresh_token|access_token|client_secret)"
            r"=(?:'[^']*'|\"[^\"]*\"|[^&\s\"'#]*)"
        ),
        rf"\1={_REDACTED}",
    ),
    (
        re.compile(
            r"(?i)([\"'](?:code|code_verifier|state|refresh_token|access_token|client_secret)[\"']"
            r"\s*:\s*[\"'])[^\"']*"
        ),
        rf"\1{_REDACTED}",
    ),
)


# Values under these keys are masked wherever they appear in a record (``extra`` fields, nested
# dicts): provider tokens, passwords, tickets, cookies and anything under ``public`` or ``secret``
# of a connection row, plus the row's key material columns and webhook secrets. The OAuth
# ``code``, ``code_verifier`` and ``state`` are masked too (so no log field is named ``state``:
# a job's state is logged as ``job_state``).
SECRET_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "authorization",
        "cookie",
        "ticket",
        "public",
        "secret_nonce",
        "wrapped_dek",
        "dek_nonce",
        "webhook_secret",
        "api_key",
        "x-api-key",
        "anthropic_api_key",
        "code",
        "code_verifier",
        "state",
        "client_secret",
    }
)


# Provider libraries and HTTP clients never log at INFO or below: request URLs can carry tokens
# and the model SDK's debug log holds request bodies.
QUIET_LOGGERS = ("anthropic", "httpx", "httpcore", "urllib3")


def redact(value: str) -> str:
    """Remove cookie values, credential headers and OAuth query strings from ``value``."""
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    return value


# What a record becomes when even the fallback line cannot be built.
LAST_RESORT_LINE = '{"level": "ERROR", "message": "a log record could not be formatted"}'


def _safe_text(value: object) -> str:
    """``str(value)``, redacted; a value that cannot be printed becomes a fixed marker."""
    try:
        return redact(str(value))
    except Exception:
        return "[unprintable]"


def _redact_key(key: Any) -> str:
    """A dict key as the string JSON will carry, redacted like a value: a key can be a webhook
    path, a cookie string or a query (``{"/api/webhooks/withings/<secret>": 3}``). A key that is
    not a string (a tuple, say, which JSON cannot encode) becomes its ``str`` first."""
    return redact(key if isinstance(key, str) else str(key))


def _redact_any(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {  # pyright: ignore[reportUnknownVariableType]
            _redact_key(k): _REDACTED
            if isinstance(k, str) and k.lower() in SECRET_KEYS
            else _redact_any(v)
            for k, v in value.items()  # pyright: ignore[reportUnknownVariableType]
        }
    if isinstance(value, list | tuple):
        return [_redact_any(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def describe_database_error(error: DBAPIError) -> str:
    """``IntegrityError (SQLSTATE 23505)``: never the message, statement or parameters."""
    state = getattr(error.orig, "sqlstate", None) or "unknown"
    return f"{type(error).__name__} (SQLSTATE {state})"


def find_database_error(error: BaseException | None) -> DBAPIError | None:
    """The first ``DBAPIError`` in ``error``'s ``__cause__``/``__context__`` chain or among the
    members of an ``ExceptionGroup`` (Starlette re-raises an error after the response started as
    a ``RuntimeError`` from the original)."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, DBAPIError):
            return current
        pending.append(current.__cause__)
        # ``raise ... from None`` (or ``from x``) sets __suppress_context__: the context was
        # hidden on purpose, so it is not followed. __cause__ still is, so a
        # DBAPIError raised ``from`` keeps being found and reduced to its SQLSTATE.
        if not current.__suppress_context__:
            pending.append(current.__context__)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)  # pyright: ignore[reportUnknownMemberType]
    return None


class JsonFormatter(logging.Formatter):
    """Formats a record as a single-line JSON object.

    ``extra={...}`` fields are included; they must never hold secrets. Every string in the
    record, keys included, goes through ``redact``.

    It fails closed: if building the line raises (a wrong number of ``%`` arguments, a value
    whose ``__str__`` raises, a structure that is too deep), the line carries the redacted
    message text, the logger and the error's type, and nothing from the arguments or the extra
    fields. Left to the stdlib, ``Handler.handleError`` would print the raw message and
    arguments to stderr.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._format(record)
        except Exception as error:
            return self._fallback(record, error)

    def _fallback(self, record: logging.LogRecord, error: Exception) -> str:
        try:
            return json.dumps(
                {
                    "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                        timespec="milliseconds"
                    ),
                    "level": _safe_text(record.levelname),
                    "logger": _safe_text(record.name),
                    "message": _safe_text(record.msg),
                    "log_error": type(error).__name__,
                },
                ensure_ascii=False,
            )
        except Exception:
            return LAST_RESORT_LINE

    def _format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and not key.startswith("_"):
                payload.setdefault(key, value)
        if record.exc_info:
            error = record.exc_info[1]
            database_error = find_database_error(error)
            if database_error is not None:
                # The message and chained driver error hold row values (DETAIL of a unique or
                # check violation: emails, hashes): class and SQLSTATE only, wherever the error
                # sits in the chain.
                outer = f"{type(error).__name__} caused by " if error is not database_error else ""
                payload["exc"] = outer + describe_database_error(database_error)
            else:
                payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(
            _redact_any(payload), default=lambda v: redact(str(v)), ensure_ascii=False
        )


class SafeStreamHandler(logging.StreamHandler[IO[str]]):
    """A stream handler that reports a write failure without the record.

    The stdlib's ``handleError`` prints the record's raw message and arguments to stderr, which
    is the container log. This one prints the error's type only.
    """

    def handleError(self, record: logging.LogRecord) -> None:
        error = sys.exc_info()[0]
        with contextlib.suppress(Exception):
            sys.stderr.write(
                json.dumps(
                    {
                        "level": "ERROR",
                        "logger": __name__,
                        "message": "a log record could not be written",
                        "log_error": error.__name__ if error is not None else "unknown",
                    }
                )
                + "\n"
            )


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> None:
    """Send all logging (ours, uvicorn's, SQLAlchemy's) to one JSON handler on the root logger."""
    handler = SafeStreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    # SQL statements carry bound parameters (token hashes, emails); even at DEBUG they are
    # never logged.
    for noisy in ("sqlalchemy.engine", "sqlalchemy.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for library in QUIET_LOGGERS:
        logging.getLogger(library).setLevel(logging.WARNING)
