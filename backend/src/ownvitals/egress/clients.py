"""The httpx clients: every request ownvitals makes over HTTPS goes through ``AllowlistTransport``
(``ownvitals.egress.transport``, re-exported here), which checks every request, each redirect
hop included, before the network transport sees it: the process role, the list, the request
budget and, with ``total_seconds``, the total deadline of the request and its answer.

Every client has ``trust_env=False`` (no proxy or ``NETRC`` from the environment), does not
follow redirects, and verifies TLS against ``certifi`` (httpx's own default) with a context that
never writes a key log. ``inner`` replaces the network transport in tests (an
``httpx.MockTransport`` or a fake, no network); every check still runs. No client takes a list:
each one's list is chosen here.

| Client | Process | Reaches |
|---|---|---|
| ``withings_async_client`` | worker | ``wbsapi.withings.net``, ``account.withings.com`` |
| ``model_async_client`` | worker, AI enabled | ``api.anthropic.com`` |
| ``google_oauth_client``, ``google_async_client`` | web | Google's token endpoint and JWKS |
| ``exchange_withings_code`` (``code_exchange``) | web | one ``POST`` (roadmap Amendment 1) |
"""

import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Final, cast

import certifi
import httpx

from ownvitals.egress.core import (
    GOOGLE_SIGNIN_ALLOWLIST,
    GOOGLE_TOKEN_HOST,
    MODEL_ALLOWLIST,
    MODEL_HOST,
    WITHINGS_API_HOST,
    WITHINGS_HOSTS,
    OutboundBlocked,
    RequestDeadline,
    check_total_seconds,
    refuse_in_worker,
    tls_client_context,
    worker_only,
)
from ownvitals.egress.transport import (  # noqa: F401 (re-exported)
    AllowlistTransport,
    Role,
)

if TYPE_CHECKING:
    from authlib.integrations.httpx_client import AsyncOAuth2Client

    class GoogleOAuthClient(AsyncOAuth2Client, httpx.AsyncClient):  # pyright: ignore[reportIncompatibleMethodOverride]
        """What ``google_oauth_client`` returns, as a type checker should see it: Authlib's
        client on httpx (Authlib's shim hides its httpx base; the helper checks it at run time).
        """


MODEL_TIMEOUT_SECONDS: Final = 120.0
# Total deadline of one model request, its answer included. A constant until the AI work
# (M7) makes it a setting; the SDK retries a request with a fresh deadline.
MODEL_TOTAL_SECONDS: Final = 300.0
# Total deadline of one Withings API request unless the caller gives one.
WITHINGS_TOTAL_SECONDS: Final = 30.0
# Total deadline of one Google sign-in request (classbeat's 10 s OAuth timeout).
GOOGLE_TOTAL_SECONDS: Final = 10.0
# The network library's exceptions a caller of these clients catches, so that it never imports
# the library (the guard). ``TRANSPORT_ERRORS``: the request failed in transport (connect, read,
# write, protocol, decoding, or a timeout); ``TIMEOUT_ERRORS``: the timeouts among them, with the
# package's own total deadline. Anything else a call raises is not a transport failure.
TRANSPORT_ERRORS: Final[tuple[type[Exception], ...]] = (httpx.RequestError, RequestDeadline)
TIMEOUT_ERRORS: Final[tuple[type[Exception], ...]] = (httpx.TimeoutException, RequestDeadline)
# The transport errors raised before any byte of the request was sent (no connection was made,
# or none was free in the pool): a caller may treat the request as never sent (the AI budget
# releases its reservation then, ADR 0014 section 7). Every other transport error may have sent it.
CONNECT_ERRORS: Final[tuple[type[Exception], ...]] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


def _network_transport() -> httpx.AsyncHTTPTransport:
    """The network transport under the checks: trust store ``certifi``, no TLS key log, no proxy.
    ``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` are ignored because the context is handed in."""
    return httpx.AsyncHTTPTransport(
        trust_env=False, verify=tls_client_context(cafile=certifi.where())
    )


def _client(transport: AllowlistTransport, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport, trust_env=False, timeout=timeout, follow_redirects=False
    )


def withings_async_client(
    *,
    total_seconds: float = WITHINGS_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The worker's Withings client (PLAN §4.2): every Withings call but the web process's
    authorization-code exchange. Refused outside the worker (``OutboundBlocked``), so the web
    process can make no refresh, revoke or other Withings call with it. Every request with its
    body ends within ``total_seconds``."""
    seconds = check_total_seconds(total_seconds)
    allowlist = worker_only(WITHINGS_HOSTS, WITHINGS_API_HOST)
    network = inner if inner is not None else _network_transport()
    return _client(
        AllowlistTransport(allowlist, network, total_seconds=seconds, role="worker"), seconds
    )


def model_async_client(
    *,
    timeout: float = MODEL_TIMEOUT_SECONDS,
    total_seconds: float = MODEL_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
    refused: Callable[[OutboundBlocked], Exception] | None = None,
) -> httpx.AsyncClient:
    """The model client (``ownvitals.ai.client`` posts the Messages API on it; no SDK, #207). Built
    only in a worker whose list holds the model host, that is with AI enabled; otherwise
    ``OutboundBlocked``. Its list is the model host alone, although the worker's is wider.
    ``timeout`` bounds each read; every request with its answer ends within ``total_seconds``."""
    seconds = check_total_seconds(total_seconds)
    allowlist = worker_only(MODEL_ALLOWLIST.hosts, MODEL_HOST)
    network = inner if inner is not None else _network_transport()
    transport = AllowlistTransport(
        allowlist, network, refused, total_seconds=seconds, role="worker"
    )
    return _client(transport, timeout)


def _google_signin_transport(
    total_seconds: float, inner: httpx.AsyncBaseTransport | None
) -> AllowlistTransport:
    """The transport of Google sign-in (web only): Google's token endpoint and JWKS hosts."""
    refuse_in_worker(GOOGLE_TOKEN_HOST)
    network = inner if inner is not None else _network_transport()
    return AllowlistTransport(
        GOOGLE_SIGNIN_ALLOWLIST, network, total_seconds=total_seconds, role="web"
    )


def google_async_client(
    *,
    total_seconds: float = GOOGLE_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Google sign-in's plain client (the JWKS fetch; web only, refused in the worker)."""
    seconds = check_total_seconds(total_seconds)
    return _client(_google_signin_transport(seconds, inner), seconds)


def google_oauth_client(
    *,
    client_id: str,
    client_secret: str,
    scope: str,
    redirect_uri: str,
    total_seconds: float = GOOGLE_TOTAL_SECONDS,
    inner: httpx.AsyncBaseTransport | None = None,
) -> "GoogleOAuthClient":
    """Authlib's ``AsyncOAuth2Client`` for Google sign-in (ADR 0003 §5; web only, refused in the
    worker), as classbeat builds it: PKCE S256, ``client_secret_post``. It runs over Google's
    sign-in transport (the token endpoint and JWKS hosts, the total deadline) with
    ``trust_env=False`` and no redirects. Outside this package nothing may import Authlib's
    integrations (the guard), so this is the only way sign-in reaches Google.

    Authlib is imported here, not at module level, so the worker never loads it. Authlib runs
    its client on ``httpx2`` when that is installed: then the client would not be an
    ``httpx.AsyncClient`` and could not take this transport, so it is refused (``TypeError``)."""
    seconds = check_total_seconds(total_seconds)
    transport = _google_signin_transport(seconds, inner)
    with warnings.catch_warnings():
        # Authlib 1.9 warns, through its own "always" filter, that its client runs on httpx
        # rather than httpx2: the filter goes in first, so this one can take precedence.
        from authlib.deprecate import AuthlibDeprecationWarning

        warnings.simplefilter("ignore", AuthlibDeprecationWarning)
        from authlib.integrations.httpx_client import AsyncOAuth2Client

    if not issubclass(AsyncOAuth2Client, httpx.AsyncClient):
        raise TypeError("Authlib's OAuth client must run on httpx")
    client = AsyncOAuth2Client(
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        redirect_uri=redirect_uri,
        token_endpoint_auth_method="client_secret_post",  # noqa: S106 - a method name
        code_challenge_method="S256",
        transport=transport,
        trust_env=False,
        timeout=seconds,
        follow_redirects=False,
    )
    return cast("GoogleOAuthClient", client)
