"""The Withings client's error taxonomy (PLAN §4.2, ADR 0004 §8).

Four classes, every one a ``WithingsError`` whose only argument is its category:

- ``TokenInvalid``: API status 401, 2553 or 2555, or HTTP 401. It goes to the token path of
  PLAN §4.3 (refresh, then ``needs_reconnect``), **never** to the generic retry: the client never
  retries it, and the refresh call itself never retries at all.
- ``RateLimited``: status 601. The client does not retry it either: the caller holds the app's
  bucket for ``wait_seconds`` (from the answer, else ``DEFAULT_RATE_LIMIT_WAIT``).
- ``Transient``: HTTP 408, 429 and 5xx, timeouts, connection errors and an answer that is not a
  JSON envelope. Retried with backoff and jitter only within the budget the caller passes.
- ``Permanent``: every other non-zero status, mapped **by number** to a fixed category
  (``STATUS_CATEGORIES``; an unlisted number is ``unknown_status``), and answers the client
  cannot use (another HTTP status, a body that does not fit the model, an oversized body).

Categories come from types, HTTP status codes and numeric statuses, never from message text. The
provider's error text is never read into an exception, a log line or a stored value. As ADR 0004
§8 asks, the client raises these after its ``except`` blocks: no ``__cause__``, no
``__context__``, ``args == (category,)``. ``endpoint`` (``"measure:getmeas"``), ``http_status``
and ``api_status`` are numbers and names only.

``interpret`` reads an answer into its model or raises the taxonomy's error (the client and the
web process's code exchange use it); ``RetryBudget`` is how many ``Transient`` retries a caller
allows; ``paginate`` follows ``more``/``offset`` and fails closed on an offset that does not
advance.
"""

import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

from pydantic import ValidationError

from ownvitals.withings.models import WithingsModel

TOKEN_INVALID_STATUSES: Final = frozenset({401, 2553, 2555})  # PLAN §4.2
RATE_LIMITED_STATUS: Final = 601
DEFAULT_RATE_LIMIT_WAIT: Final = 60.0  # PLAN §4.4: "pauses the bucket for wait_seconds or 60 s"
TRANSIENT_HTTP: Final = frozenset({408, 429})  # and every 5xx

# The fixed list of permanent categories by API status. Numbers from the public docs' status
# families and the fake's readings (docs/status.md, "Blocked on the owner"): 214 is the assumed
# answer to a missing scope, which capability discovery reads as ``not_authorized``.
STATUS_CATEGORIES: Final[dict[int, str]] = {
    214: "not_authorized",
    247: "invalid_userid",
    286: "no_such_subscription",
    293: "invalid_callback",
    304: "invalid_code",
    503: "invalid_params",
    2554: "not_implemented",
}
UNKNOWN_STATUS: Final = "unknown_status"

# Categories that do not come from an API status.
TOKEN_INVALID: Final = "token_invalid"  # noqa: S105 - a category name
RATE_LIMITED: Final = "rate_limited"
HTTP_STATUS: Final = "http_status"  # Transient (408, 429, 5xx) or Permanent (any other)
TIMEOUT: Final = "timeout"
NETWORK: Final = "network"
MALFORMED: Final = "malformed_response"  # not a JSON object with an integer status
UNEXPECTED_BODY: Final = "unexpected_body"  # status 0, but the body does not fit the model
TOO_LARGE: Final = "response_too_large"
PAGINATION: Final = "pagination_stuck"


class WithingsError(Exception):
    """A failed Withings call. ``args == (category,)``; nothing else reaches ``str``."""

    def __init__(
        self,
        category: str,
        *,
        endpoint: str = "",
        http_status: int | None = None,
        api_status: int | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.endpoint = endpoint
        self.http_status = http_status
        self.api_status = api_status

    @property
    def ambiguous(self) -> bool:
        """Whether Withings may have acted on the request although no usable answer came back.
        For a token refresh this means "the refresh token may have rotated" (PLAN §4.3 step 4)."""
        return False


class TokenInvalid(WithingsError):
    """The token was refused (401, 2553, 2555): PLAN §4.3's path, never the generic retry."""


class RateLimited(WithingsError):
    """Status 601: the app's bucket holds for ``wait_seconds``."""

    def __init__(
        self,
        category: str,
        *,
        wait_seconds: float,
        endpoint: str = "",
        http_status: int | None = None,
        api_status: int | None = None,
    ) -> None:
        super().__init__(
            category, endpoint=endpoint, http_status=http_status, api_status=api_status
        )
        self.wait_seconds = wait_seconds


class Transient(WithingsError):
    """A timeout, a connection error, HTTP 408, 429 or 5xx, or an unreadable answer."""

    @property
    def ambiguous(self) -> bool:
        return True


class Permanent(WithingsError):
    """A categorized failure that a retry does not fix."""

    @property
    def ambiguous(self) -> bool:
        # Status 0 with a body that does not fit: Withings did act (a refresh did rotate).
        return self.category == UNEXPECTED_BODY


def status_error(status: int, endpoint: str, wait_seconds: float | None = None) -> WithingsError:
    """The error for a non-zero API ``status`` (an HTTP 200 answer)."""
    if status in TOKEN_INVALID_STATUSES:
        return TokenInvalid(TOKEN_INVALID, endpoint=endpoint, http_status=200, api_status=status)
    if status == RATE_LIMITED_STATUS:
        wait = wait_seconds if wait_seconds is not None else DEFAULT_RATE_LIMIT_WAIT
        return RateLimited(
            RATE_LIMITED, wait_seconds=wait, endpoint=endpoint, http_status=200, api_status=status
        )
    category = STATUS_CATEGORIES.get(status, UNKNOWN_STATUS)
    return Permanent(category, endpoint=endpoint, http_status=200, api_status=status)


def http_error(http_status: int, endpoint: str) -> WithingsError:
    """The error for an HTTP status other than 200."""
    if http_status == 401:
        return TokenInvalid(TOKEN_INVALID, endpoint=endpoint, http_status=http_status)
    if http_status in TRANSIENT_HTTP or 500 <= http_status <= 599:
        return Transient(HTTP_STATUS, endpoint=endpoint, http_status=http_status)
    return Permanent(HTTP_STATUS, endpoint=endpoint, http_status=http_status)


# ------------------------------------------------- retries and reading an answer

BACKOFF_BASE_SECONDS: Final = 1.0
JITTER: Final = 0.2  # +-20 %


@dataclass
class RetryBudget:
    """How many ``Transient`` retries a caller allows, shared by every call it is passed to.
    No single wait is longer than ``max_delay``, and none ends after ``deadline``
    (``time.monotonic()`` seconds) when one is set."""

    retries: int = 0
    max_delay: float = 60.0
    deadline: float | None = None
    used: int = 0

    def next_delay(self, rng: random.Random, now: float, hint: float | None = None) -> float | None:
        """The wait before the next retry, spending one; None when the budget is spent."""
        if self.retries <= 0:
            return None
        delay = min(BACKOFF_BASE_SECONDS * 2**self.used, self.max_delay)
        delay *= 1 + rng.uniform(-JITTER, JITTER)
        if hint is not None and hint > 0:
            delay = max(delay, min(hint, self.max_delay))
        if self.deadline is not None and now + delay > self.deadline:
            return None
        self.retries -= 1
        self.used += 1
        return delay


def _refuse_constant(name: str) -> Any:
    raise ValueError("NaN and Infinity are not JSON numbers")


def _wait_seconds(payload: Mapping[str, Any]) -> float | None:
    body = payload.get("body")
    wait = body.get("wait_seconds") if isinstance(body, dict) else None
    if isinstance(wait, int | Decimal) and not isinstance(wait, bool) and wait > 0:
        return float(wait)
    return None


def interpret[M: WithingsModel](endpoint: str, http_status: int, body: bytes, model: type[M]) -> M:
    """An answer read into ``model``, or the taxonomy's error. Used by the client and by the
    web process's code exchange (#22), which reads ``egress.exchange_withings_code``'s answer
    with ``interpret("v2/oauth2:requesttoken", status, body, TokenResponse)``."""
    if http_status != 200:
        raise http_error(http_status, endpoint)
    failure: WithingsError | None = None
    payload: Any = None
    try:
        payload = json.loads(body, parse_float=Decimal, parse_constant=_refuse_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        failure = Transient(MALFORMED, endpoint=endpoint, http_status=http_status)
    if failure is not None:
        raise failure
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, int) or isinstance(status, bool):
        raise Transient(MALFORMED, endpoint=endpoint, http_status=http_status)
    if status != 0:
        raise status_error(status, endpoint, _wait_seconds(payload))
    result: M | None = None
    try:
        result = model.model_validate(payload.get("body", {}))
    except ValidationError:  # its text holds values: only the category is kept
        result = None
    if result is None:
        raise Permanent(UNEXPECTED_BODY, endpoint=endpoint, http_status=200, api_status=0)
    return result


MAX_PAGES: Final = 10_000


class _Page(Protocol):
    @property
    def more(self) -> bool: ...
    @property
    def offset(self) -> int: ...


async def paginate[P: _Page](
    fetch: Callable[[int | None], Awaitable[P]], *, max_pages: int = MAX_PAGES
) -> AsyncIterator[P]:
    """Every page of an offset-paged endpoint: ``fetch(None)`` first, then ``fetch(offset)``
    while ``more`` is set. An offset that does not advance, or more than ``max_pages`` pages,
    raises ``Permanent("pagination_stuck")`` instead of looping."""
    offset: int | None = None
    for _ in range(max_pages):
        page = await fetch(offset)
        yield page
        if not page.more:
            return
        if page.offset <= (offset or 0):
            raise Permanent(PAGINATION)
        offset = page.offset
    raise Permanent(PAGINATION)


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "DEFAULT_RATE_LIMIT_WAIT",
    "JITTER",
    "MALFORMED",
    "MAX_PAGES",
    "NETWORK",
    "PAGINATION",
    "STATUS_CATEGORIES",
    "TIMEOUT",
    "TOKEN_INVALID_STATUSES",
    "TOO_LARGE",
    "UNEXPECTED_BODY",
    "UNKNOWN_STATUS",
    "Permanent",
    "RateLimited",
    "RetryBudget",
    "TokenInvalid",
    "Transient",
    "WithingsError",
    "http_error",
    "interpret",
    "paginate",
    "status_error",
]
