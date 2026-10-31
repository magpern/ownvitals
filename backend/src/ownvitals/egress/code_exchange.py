"""The web process's one Withings call: the authorization-code exchange (roadmap Amendment 1,
ADR 0004 §7, PLAN §4.3).

The OAuth callback must exchange the code within its 30 s validity, in the request, so this one
call runs in web. ``withings_code_exchange_client`` is the only Withings entry point the web
process has; it is refused in the worker (``refuse_in_worker``), which makes the refresh, revoke
and every other Withings call through ``withings_async_client``. Its transport admits exactly:

- ``POST https://wbsapi.withings.net/v2/oauth2``: no other method, host, port, path or query
  (the raw path is compared, so ``/v2/oauth2/``, ``/v2/%6Fauth2`` or ``?x=`` are refused);
- a form body (``application/x-www-form-urlencoded``, not content-encoded, at most 4 KiB) whose
  fields are each present at most once and all among ``action``, ``grant_type``, ``client_id``,
  ``client_secret``, ``code`` and ``redirect_uri``, with ``action=requesttoken``,
  ``grant_type=authorization_code`` and a non-empty ``code``. Unknown fields are refused, not
  ignored: a server that folds ``grant.type`` or ``grant_type[]`` into ``grant_type`` must
  never see a second grant type;
- no redirect: any redirect answer is refused (``after_request``), never followed.

Anything else raises ``OutboundBlocked`` before the network transport sees the request, and
logs the host only: never the form, which holds the code and the client secret. The call counts
against the app's Withings rate budget like any other (#23 wires it).
"""

from typing import Final
from urllib.parse import parse_qsl

import httpx

from ownvitals.egress.clients import AllowlistTransport, _network_transport
from ownvitals.egress.core import (
    CODE_EXCHANGE_ALLOWLIST,
    WITHINGS_API_HOST,
    WITHINGS_TOKEN_PATH,
    blocked,
    check_total_seconds,
    refuse_in_worker,
)

# The code is valid for 30 s; the exchange gets a third of it.
CODE_EXCHANGE_TOTAL_SECONDS: Final = 10.0
CODE_EXCHANGE_FIELDS: Final = frozenset(
    {"action", "grant_type", "client_id", "client_secret", "code", "redirect_uri"}
)
_FORM_TYPE: Final = "application/x-www-form-urlencoded"
_MAX_BODY_BYTES: Final = 4096
_RAW_PATH: Final = WITHINGS_TOKEN_PATH.encode("ascii")


def is_code_exchange_form(body: bytes) -> bool:
    """Whether ``body`` is an authorization-code exchange form and nothing more."""
    if len(body) > _MAX_BODY_BYTES:
        return False
    try:
        pairs = parse_qsl(
            body.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=len(CODE_EXCHANGE_FIELDS),
        )
    except ValueError:  # not ASCII (UnicodeDecodeError), malformed, or too many fields
        return False
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names) or not set(names) <= CODE_EXCHANGE_FIELDS:
        return False
    form = dict(pairs)
    return (
        form.get("action") == "requesttoken"
        and form.get("grant_type") == "authorization_code"
        and bool(form.get("code"))
    )


class _CodeExchangeTransport(AllowlistTransport):
    async def _admit(self, request: httpx.Request) -> None:
        headers = request.headers
        if (
            request.method != "POST"
            or request.url.raw_path != _RAW_PATH
            or headers.get("content-type", "").lower() != _FORM_TYPE
            or "content-encoding" in headers
        ):
            raise blocked(WITHINGS_API_HOST)
        if not is_code_exchange_form(await request.aread()):
            raise blocked(WITHINGS_API_HOST)

    def _redirect_allowed(self, url: httpx.URL, target: httpx.URL | None) -> bool:
        return False


def withings_code_exchange_client(
    *,
    total_seconds: float = CODE_EXCHANGE_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The web process's client for the authorization-code exchange (module docstring). Refused
    in the worker; every request it sends is checked to be that one exchange, and ends with its
    answer's body within ``total_seconds``."""
    seconds = check_total_seconds(total_seconds)
    refuse_in_worker(WITHINGS_API_HOST)
    network = inner if inner is not None else _network_transport()
    transport = _CodeExchangeTransport(
        CODE_EXCHANGE_ALLOWLIST, network, total_seconds=seconds, role="web"
    )
    return httpx.AsyncClient(
        transport=transport, trust_env=False, timeout=seconds, follow_redirects=False
    )
