"""Wire-level pieces of the Withings fake: constants, the request and response types, the clock,
the signature helper, the webhook form and the JSON response builders. No behaviour of its own;
``withingscore`` re-exports the names so callers import everything from there. Synthetic only,
standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlsplit

from withingsdata import SyntheticDataset
from withingsstreams import START

API_HOST = "wbsapi.withings.net"
ACCOUNT_HOST = "account.withings.com"
AUTHORIZE_PATH = "/oauth2_user/authorize2"
SYNTHETIC_HEADER = "x-ownvitals-synthetic"  # on every response: the data is invented
CODE_TTL = 30  # seconds (findings section 2)
ACCESS_TTL = 10_800  # 3 h
REFRESH_TTL = 365 * 86_400  # 1 year
REFRESH_GRACE = 8 * 3_600  # the old refresh token stays valid up to 8 h
NONCE_TTL = 30 * 60  # public OpenAPI file
SUBSCRIPTION_CANCEL_AFTER = 20 * 86_400  # failing for 20 days cancels it
WEBHOOK_RETRY_DELAYS = (10, 60, 3_600, 14_400)  # after the first cycle; 2 attempts per cycle
WEBHOOK_ATTEMPTS_PER_CYCLE = 2
WEBHOOK_RESPONSE_TIMEOUT = 3.0  # seconds; findings section 2 says "a few seconds" (assumed, short)
SCOPES = ("user.info", "user.metrics", "user.activity", "user.sleepevents")
DEMO_USERID = 9_000_001

OK = 0
STATUS_INVALID_TOKEN = 401  # documented family "Authentication failed"
STATUS_BAD_REFRESH_TOKEN = 2553  # family "Unauthorized"; assumed for a bad refresh token
STATUS_NO_SCOPE = 214  # family "Unauthorized"; assumed for a missing scope (not documented)
STATUS_BAD_CODE = 304  # "authorization code absent or incorrect" (llms.md)
STATUS_BAD_PARAMS = 503  # family "Invalid params"
STATUS_BAD_USERID = 247  # "Invalid userid" (llms.md)
STATUS_BAD_CALLBACK = 293  # "callback URL absent or incorrect" (llms.md)
STATUS_NO_SUBSCRIPTION = 286  # "No such subscription" (llms.md)
STATUS_NOT_IMPLEMENTED = 2554  # family "Not implemented"
STATUS_RATE_LIMITED = 601
STATUS_UNKNOWN = 2555  # family "Unauthorized" (the default injected status)

APPLI_SCOPE: dict[int, str] = {  # notification content page; appli 53 has no user
    **dict.fromkeys((1, 2, 4, 54, 55, 58, 61, 62, 63), "user.metrics"),
    **dict.fromkeys((16, 44), "user.activity"),
    46: "user.info",
    **dict.fromkeys((50, 51, 52, 98, 99, 100), "user.sleepevents"),
}
MASKED = frozenset(  # the log never shows these; callbackurl embeds the webhook secret
    {
        "client_secret", "refresh_token", "access_token", "code", "signature", "nonce",
        "callbackurl", "new_callbackurl",
    }
)  # fmt: skip


class Clock(Protocol):
    def now(self) -> float: ...


class ManualClock:
    """Epoch seconds that only move when the test says so (starts at 2026-10-31T00:00:00Z)."""

    def __init__(self, start: float = START) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def set(self, timestamp: float) -> None:
        self._now = float(timestamp)


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()

    @property
    def path(self) -> str:
        return urlsplit(self.url).path

    def query_params(self) -> dict[str, str]:
        return dict(parse_qsl(urlsplit(self.url).query, keep_blank_values=True))

    def form_params(self) -> dict[str, str]:
        return dict(parse_qsl(self.body.decode("utf-8", "replace"), keep_blank_values=True))

    def params(self) -> dict[str, str]:
        """Query string and form body merged (the body wins). The API reads ``form_params``."""
        return {**self.query_params(), **self.form_params()}

    def bearer(self) -> str | None:
        value = self.headers.get("authorization", "")
        return value[7:].strip() if value.lower().startswith("bearer ") else None


@dataclass
class Response:
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    delay: float = 0.0  # seconds the response is held back (the transport applies it)

    def json(self) -> Any:
        return json.loads(self.body)


def json_response(payload: Mapping[str, Any]) -> Response:
    headers = {"content-type": "application/json", SYNTHETIC_HEADER: "true"}
    return Response(200, headers, json.dumps(payload, separators=(",", ":")).encode())


def ok_response(body: Mapping[str, Any]) -> Response:
    return json_response({"status": OK, "body": body})


def error_response(status: int, text: str, wait_seconds: int | None = None) -> Response:
    """An API error: HTTP 200 with a non-zero ``status`` (601 may carry ``wait_seconds``)."""
    payload: dict[str, Any] = {"status": status, "error": text}
    if wait_seconds is not None:
        payload["body"] = {"wait_seconds": wait_seconds}
    return json_response(payload)


class ConnectionDropped(Exception):
    """The connection broke; ``processed`` says whether the server had handled the request."""

    def __init__(self, processed: bool) -> None:
        super().__init__("connection dropped")
        self.processed = processed


class Unroutable(Exception):
    """The request was addressed to a host the fake does not answer for."""


def sign(secret: str, *values: object) -> str:
    """The signature of a signed call: HMAC-SHA256 over the values joined by commas, hex."""
    message = ",".join(map(str, values)).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def webhook_form(
    appli: int,
    userid: int | None = None,
    *,
    startdate: int | None = None,
    enddate: int | None = None,
    date: str | int | None = None,
    deviceid: str = "",
    action: str = "update",
    mac: str = "",
) -> dict[str, str]:
    """The form body Withings POSTs for ``appli`` (notification content page of the docs):
    a time range for 1, 2, 4, 44, 54, 58, 61 to 63; ``date`` (YYYY-MM-DD) for 16; ``action`` for
    46; ``date`` and ``deviceid`` for 50 to 52, 55 and 98 to 100; ``mac`` only for 53."""
    if appli == 53:
        return {"appli": "53", "mac": mac}
    form = {"userid": str(userid), "appli": str(appli)}
    if appli in (16, 50, 51, 52, 55, 98, 99, 100):
        form["date"] = str(date if date is not None else enddate)
        if appli != 16:
            form["deviceid"] = deviceid
    elif appli == 46:
        form["action"] = action
    elif appli in APPLI_SCOPE:
        form.update(startdate=str(startdate), enddate=str(enddate))
    else:
        raise ValueError(f"unknown appli {appli}")
    return form


# ----------------------------------------------------------------- state records of the fake

FailureKind = Literal["drop", "delay", "status", "http", "garbage", "mutate"]


@dataclass
class App:
    client_id: str
    client_secret: str
    redirect_uris: frozenset[str]


@dataclass
class FakeUser:
    userid: int
    data: SyntheticDataset


@dataclass
class AuthCode:
    client_id: str
    userid: int
    redirect_uri: str
    scope: str
    expires_at: float
    used: bool = False


@dataclass
class Grant:
    """One user's authorization of one app: the live token pair and the previous refresh token."""

    client_id: str
    userid: int
    scope: str = ""
    generation: int = 0
    access: str = ""
    access_expires: float = 0.0
    access_used: bool = False
    refresh: str = ""
    refresh_expires: float = 0.0
    previous_refresh: str | None = None
    previous_valid_until: float = 0.0


@dataclass
class Subscription:
    client_id: str
    userid: int
    appli: int
    callbackurl: str
    comment: str = ""
    failing_since: float | None = None


@dataclass
class Failure:
    """Misbehave on given request numbers. ``at`` counts every request, or, with ``endpoint``
    (``"v2/oauth2:requesttoken"``, ``"measure:getmeas"``...), the requests of that endpoint.
    ``when="after"`` lets the server process the request first (state changes, the answer is
    lost or replaced), ``"before"`` leaves state alone."""

    kind: FailureKind
    at: frozenset[int]
    endpoint: str | None = None
    when: Literal["before", "after"] = "before"
    status: int = STATUS_UNKNOWN
    wait_seconds: int | None = None
    http_status: int = 500
    seconds: float = 0.0
    action: Callable[[Any], None] | None = None  # called with the FakeWithings
    fired: int = 0


@dataclass
class LogEntry:
    number: int
    endpoint: str
    params: dict[str, str]  # secrets masked
    at: float
    api_status: int | None = None
    http_status: int | None = None
    outcome: str = "answered"  # or "dropped"
    method: str = ""
    query_names: tuple[str, ...] = ()  # parameters that came in the URL's query string
