"""The web process's Cloudflare Turnstile verification (roadmap Amendment 12, ADR 0018).

When the operator turns Turnstile on (``turnstile_enabled``), sign-up, sign-in and the reset
request carry a widget token that web checks at Cloudflare's ``siteverify`` endpoint before it
does anything else. ``verify_turnstile_token`` is that call and the only way to it. It is refused
in the worker (``refuse_in_worker``); with Turnstile off no caller exists, and the runtime hook
does not even resolve the host (``egress.hook``).

The caller hands in two values; **this module builds the request**, so no caller can pass a body,
a header or a URL:

- the secret and the token are non-empty strings of printable ASCII without a space, ``;``,
  ``&``, ``=`` or ``#`` (so no CR, LF or other control character), at most 256 and 2048
  characters (Cloudflare's documented token limit), refused before anything is encoded;
- the body is ``urlencode`` of exactly ``secret`` and ``response``, nothing else: no
  ``remoteip``, so the visitor's address is not sent (Cloudflare sees it from the widget anyway);
- the headers are ``Content-Type`` (form), ``Accept``, ``User-Agent`` and httpx's own ``Host`` and
  ``Content-Length``.

The transport checks the **final bytes on the wire** again: ``POST
https://challenges.cloudflare.com/turnstile/v0/siteverify`` (no other method, host, port, raw
path or query), only those header names (each once), and a body of at most 4 KiB that is the
canonical ``urlencode`` of exactly the two fields, each clean. Any redirect is refused, never
followed. Anything else raises ``OutboundBlocked`` before the network transport sees the request
and logs the host only: never a value (the secret, the token).
"""

from typing import Final
from urllib.parse import parse_qsl, urlencode

import httpx

from ownvitals.egress.clients import AllowlistTransport, _network_transport
from ownvitals.egress.code_exchange import _HEADERS as _EXCHANGE_HEADERS
from ownvitals.egress.core import (
    TURNSTILE_ALLOWLIST,
    TURNSTILE_HOST,
    TURNSTILE_VERIFY_PATH,
    TURNSTILE_VERIFY_URL,
    blocked,
    check_total_seconds,
    refuse_in_worker,
)

# A sign-in waits for this at most; a slower answer is a refusal (fail closed).
TURNSTILE_TOTAL_SECONDS: Final = 5.0
TURNSTILE_FIELDS: Final = ("secret", "response")
TURNSTILE_TOKEN_MAX: Final = 2048
TURNSTILE_SECRET_MAX: Final = 256
TURNSTILE_HEADERS: Final = frozenset(
    {"content-type", "accept", "user-agent", "host", "content-length"}
)
_FORM_TYPE: Final = "application/x-www-form-urlencoded"
_HEADERS: Final = {
    "Content-Type": _FORM_TYPE,
    "Accept": "application/json",
    # The code exchange's own value: the package's name as a string stays in its one pinned
    # position (``tests/user_context_rules.py``, ``PACKAGE_STRING_PINS``).
    "User-Agent": _EXCHANGE_HEADERS["User-Agent"],
}
_FORBIDDEN: Final = frozenset(";&=#")
_MAX_BODY_BYTES: Final = 4096
_RAW_PATH: Final = TURNSTILE_VERIFY_PATH.encode("ascii")
_LIMITS: Final = {"secret": TURNSTILE_SECRET_MAX, "response": TURNSTILE_TOKEN_MAX}


def clean_value(value: object, limit: int) -> bool:
    """A non-empty string of at most ``limit`` printable ASCII characters (``!`` to ``~``)
    without ``;``, ``&``, ``=`` or ``#``."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and all("!" <= c <= "~" and c not in _FORBIDDEN for c in value)
    )


def is_turnstile_form(body: bytes) -> bool:
    """Whether ``body`` is exactly a siteverify form (module docstring), checked on the bytes."""
    if len(body) > _MAX_BODY_BYTES:
        return False
    try:
        text = body.decode("ascii")
        pairs = parse_qsl(
            text, keep_blank_values=True, strict_parsing=True, max_num_fields=len(TURNSTILE_FIELDS)
        )
    except ValueError:  # not ASCII, malformed, or too many fields
        return False
    if [name for name, _ in pairs] != list(TURNSTILE_FIELDS):
        return False
    if urlencode(pairs) != text:  # canonical: no raw ";", "#", CR, LF ...
        return False
    return all(clean_value(value, _LIMITS[name]) for name, value in pairs)


def encode_turnstile_form(*, secret: str, token: str) -> bytes:
    """The siteverify body: refused (``OutboundBlocked``, the host only) unless both values are
    clean and the encoded body is exactly the two fields."""
    fields = [("secret", secret), ("response", token)]
    if not all(clean_value(value, _LIMITS[name]) for name, value in fields):
        raise blocked(TURNSTILE_HOST)
    body = urlencode(fields).encode("ascii")
    if not is_turnstile_form(body):
        raise blocked(TURNSTILE_HOST)
    return body


def _headers_allowed(request: httpx.Request, body: bytes) -> bool:
    names = [name.lower() for name, _ in request.headers.multi_items()]
    headers = request.headers
    return (
        len(set(names)) == len(names)
        and set(names) <= TURNSTILE_HEADERS
        and headers.get("host") == TURNSTILE_HOST
        and headers.get("content-type") == _FORM_TYPE
        and headers.get("content-length") == str(len(body))
    )


class _TurnstileTransport(AllowlistTransport):
    async def _admit(self, request: httpx.Request) -> None:
        if request.method != "POST" or request.url.raw_path != _RAW_PATH:
            raise blocked(TURNSTILE_HOST)
        body = await request.aread()
        if not _headers_allowed(request, body) or not is_turnstile_form(body):
            raise blocked(TURNSTILE_HOST)

    def _redirect_allowed(self, url: httpx.URL, target: httpx.URL | None) -> bool:
        return False


async def verify_turnstile_token(
    *,
    secret: str,
    token: str,
    total_seconds: float = TURNSTILE_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """Post one widget token to Turnstile's siteverify (web only; module docstring). Returns the
    answer with its body read, within ``total_seconds``; the caller judges it
    (``ownvitals.auth.turnstile_policy``)."""
    seconds = check_total_seconds(total_seconds)
    refuse_in_worker(TURNSTILE_HOST)
    body = encode_turnstile_form(secret=secret, token=token)
    network = inner if inner is not None else _network_transport()
    transport = _TurnstileTransport(TURNSTILE_ALLOWLIST, network, total_seconds=seconds, role="web")
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, timeout=seconds, follow_redirects=False
    ) as client:
        # Built here and sent as is: ``send`` adds none of the client's default headers.
        request = httpx.Request("POST", TURNSTILE_VERIFY_URL, content=body, headers=_HEADERS)
        return await client.send(request)
