"""The web process's one Withings call: the authorization-code exchange (roadmap Amendment 1,
ADR 0004 §7, PLAN §4.3).

The OAuth callback must exchange the code within its 30 s validity, in the request, so this one
call runs in web. ``exchange_withings_code`` is the only Withings entry point the web process has;
it is refused in the worker (``refuse_in_worker``), which makes the refresh, revoke and every
other Withings call through ``withings_async_client``.

The caller hands in the four values; **this module builds the request**, so no caller can pass a
body, a header or a URL:

- every value is a non-empty string of printable ASCII without a space, ``;``, ``&``, ``=`` or
  ``#`` (so no CR, LF or other control character), refused before anything is encoded: a server
  that splits on ``;`` or stops at ``#`` must never see a second field;
- the body is ``urlencode`` of exactly ``action=requesttoken``, ``grant_type=authorization_code``,
  ``client_id``, ``client_secret``, ``code`` and ``redirect_uri``, and its parsed form must equal
  that field list exactly;
- the headers are ``Content-Type`` (form), ``Accept``, ``User-Agent`` and httpx's own ``Host`` and
  ``Content-Length``, nothing else.

The transport checks the **final bytes on the wire** again, whatever reaches it: ``POST
https://wbsapi.withings.net/v2/oauth2`` (no other method, host, port, raw path or query), only
the header names above (each once, ``Host`` the API host, the form content type, a matching
``Content-Length``), and a body of at most 4 KiB that is the canonical ``urlencode`` of its own
parsed fields, with exactly the allowed fields, each at most once, the fixed action and grant
type, a non-empty code and no forbidden character in any decoded value. Any redirect answer is
refused, never followed.

Anything else raises ``OutboundBlocked`` before the network transport sees the request, and logs
the host only: never a value, which holds the code and the client secret. The call counts against
the app's Withings rate budget once the rate limiter exists (#23).
"""

from typing import Final
from urllib.parse import parse_qsl, urlencode

import httpx

from ownvitals.egress.clients import AllowlistTransport, _network_transport
from ownvitals.egress.core import (
    CODE_EXCHANGE_ALLOWLIST,
    WITHINGS_API_HOST,
    WITHINGS_TOKEN_PATH,
    WITHINGS_TOKEN_URL,
    blocked,
    check_total_seconds,
    refuse_in_worker,
)

# The code is valid for 30 s; the exchange gets a third of it.
CODE_EXCHANGE_TOTAL_SECONDS: Final = 10.0
# The body's fields, in order.
CODE_EXCHANGE_FIELDS: Final = (
    "action",
    "grant_type",
    "client_id",
    "client_secret",
    "code",
    "redirect_uri",
)
# The request's header names (lower case): ours and httpx's own Host and Content-Length.
CODE_EXCHANGE_HEADERS: Final = frozenset(
    {"content-type", "accept", "user-agent", "host", "content-length"}
)
_FORM_TYPE: Final = "application/x-www-form-urlencoded"
_HEADERS: Final = {
    "Content-Type": _FORM_TYPE,
    "Accept": "application/json",
    "User-Agent": "ownvitals",
}
_FORBIDDEN: Final = frozenset(";&=#")
_MAX_BODY_BYTES: Final = 4096
_RAW_PATH: Final = WITHINGS_TOKEN_PATH.encode("ascii")


def _clean(value: object) -> bool:
    """A non-empty string of printable ASCII (``!`` to ``~``) without ``;``, ``&``, ``=`` or
    ``#``: no space, no CR, LF or other control character, nothing outside ASCII."""
    return (
        isinstance(value, str)
        and value != ""
        and all("!" <= c <= "~" and c not in _FORBIDDEN for c in value)
    )


def is_code_exchange_form(body: bytes) -> bool:
    """Whether ``body`` is an authorization-code exchange form and nothing more (module
    docstring): checked on the bytes, so it holds for what goes on the wire."""
    if len(body) > _MAX_BODY_BYTES:
        return False
    try:
        text = body.decode("ascii")
        pairs = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(CODE_EXCHANGE_FIELDS),
        )
    except ValueError:  # not ASCII (UnicodeDecodeError), malformed, or too many fields
        return False
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names) or not set(names) <= set(CODE_EXCHANGE_FIELDS):
        return False
    if urlencode(pairs) != text:  # canonical: no raw ";", "#", CR, LF, "%20" for "+" ...
        return False
    form = dict(pairs)
    return (
        form.get("action") == "requesttoken"
        and form.get("grant_type") == "authorization_code"
        and all(_clean(value) for value in form.values())
        and "code" in form
    )


def encode_code_exchange(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> bytes:
    """The exchange's body: refused (``OutboundBlocked``, the host only) unless every value is
    clean and the encoded body parses back to exactly these fields."""
    fields = [
        ("action", "requesttoken"),
        ("grant_type", "authorization_code"),
        ("client_id", client_id),
        ("client_secret", client_secret),
        ("code", code),
        ("redirect_uri", redirect_uri),
    ]
    if not all(_clean(value) for _, value in fields):
        raise blocked(WITHINGS_API_HOST)
    body = urlencode(fields).encode("ascii")
    if parse_qsl(body.decode("ascii"), keep_blank_values=True) != fields:
        raise blocked(WITHINGS_API_HOST)
    if not is_code_exchange_form(body):
        raise blocked(WITHINGS_API_HOST)
    return body


def _headers_allowed(request: httpx.Request, body: bytes) -> bool:
    names = [name.lower() for name, _ in request.headers.multi_items()]
    headers = request.headers
    return (
        len(set(names)) == len(names)
        and set(names) <= CODE_EXCHANGE_HEADERS
        and headers.get("host") == WITHINGS_API_HOST
        and headers.get("content-type") == _FORM_TYPE
        and headers.get("content-length") == str(len(body))
    )


class _CodeExchangeTransport(AllowlistTransport):
    async def _admit(self, request: httpx.Request) -> None:
        if request.method != "POST" or request.url.raw_path != _RAW_PATH:
            raise blocked(WITHINGS_API_HOST)
        body = await request.aread()
        if not _headers_allowed(request, body) or not is_code_exchange_form(body):
            raise blocked(WITHINGS_API_HOST)

    def _redirect_allowed(self, url: httpx.URL, target: httpx.URL | None) -> bool:
        return False


def _code_exchange_transport(
    total_seconds: float, inner: httpx.AsyncBaseTransport | None
) -> _CodeExchangeTransport:
    network = inner if inner is not None else _network_transport()
    return _CodeExchangeTransport(
        CODE_EXCHANGE_ALLOWLIST, network, total_seconds=total_seconds, role="web"
    )


async def exchange_withings_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    total_seconds: float = CODE_EXCHANGE_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Exchange an authorization code at Withings (web only; module docstring). Returns the
    answer with its body read, within ``total_seconds``; Withings reports errors in the JSON
    ``status`` of an HTTP 200, which the caller checks."""
    seconds = check_total_seconds(total_seconds)
    refuse_in_worker(WITHINGS_API_HOST)
    body = encode_code_exchange(
        client_id=client_id, client_secret=client_secret, code=code, redirect_uri=redirect_uri
    )
    transport = _code_exchange_transport(seconds, inner)
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, timeout=seconds, follow_redirects=False
    ) as client:
        # Built here and sent as is: ``send`` adds none of the client's default headers.
        request = httpx.Request("POST", WITHINGS_TOKEN_URL, content=body, headers=_HEADERS)
        return await client.send(request)
